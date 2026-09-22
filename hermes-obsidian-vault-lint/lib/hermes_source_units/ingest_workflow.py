"""Vault-authoritative ingest workflow ledger; dispatch is a separate adapter."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

from .knowledge_build import FileKnowledgeBuildService
from .source_units import (_exclusive_lock, _json_bytes, _load_json,
                           _vault_path, _write_atomic)
from .validation import ContractError, fingerprint, validate_record

WORKFLOW_ROOT = "_system/ledgers/ingest-workflows"
WORKFLOW_CONTRACT = "hermes-ingest-workflow/v1"
STAGES = ("created", "source_preparing", "planning", "analyzing", "reducing",
          "checkpoint_1", "build_finalizing", "release_planning", "checkpoint_2",
          "applying", "indexing", "validating", "completed")
TERMINAL = {"completed", "partial", "failed", "cancelled"}
WORKER_KINDS = (
    "source-prepare", "exact-plan", "pass-slice", "resource-reduce",
    "global-reduce", "checkpoint-1-validate", "build-finalize",
    "vault-finalize-plan", "checkpoint-2-validate", "release-apply",
    "provider-sync", "acceptance",
)
_ID = re.compile(r"^ingest-[A-Za-z0-9][A-Za-z0-9_.:-]{0,152}$")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


def mutation_digest(request: Mapping[str, Any]) -> str:
    """Digest of the complete mutation request except its digest field."""
    return "sha256:" + fingerprint({k: v for k, v in request.items()
                                    if k != "input_digest"})


class FileIngestWorkflowService:
    def __init__(self, vault_root: str | Path):
        self.knowledge = FileKnowledgeBuildService(vault_root)
        self.vault = self.knowledge.vault

    def _path(self, workflow_id: str) -> Path:
        if not _ID.fullmatch(workflow_id):
            _fail("INVALID_SCHEMA", "invalid workflow id", "$.workflow_id")
        return _vault_path(self.vault, f"{WORKFLOW_ROOT}/{workflow_id}.json")

    def _lock(self, workflow_id: str) -> Path:
        self._path(workflow_id)
        return _vault_path(self.vault, f"{WORKFLOW_ROOT}/.locks/{workflow_id}.lock")

    def _load(self, workflow_id: str) -> dict[str, Any]:
        value = _load_json(self._path(workflow_id))
        validate_record("ingest_workflow", value)
        if value["workflow_id"] != workflow_id:
            _fail("INVALID_SCHEMA", "workflow file identity mismatch")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        validate_record("ingest_workflow", value)
        _write_atomic(self._path(str(value["workflow_id"])), _json_bytes(value))

    @staticmethod
    def _check_request(request: Mapping[str, Any]) -> None:
        if not isinstance(request.get("actor"), str) or not request["actor"].strip():
            _fail("INVALID_SCHEMA", "actor is required", "$.actor")
        if type(request.get("expected_revision")) is not int or request["expected_revision"] < 0:
            _fail("INVALID_SCHEMA", "expected_revision is required", "$.expected_revision")
        if request.get("input_digest") != mutation_digest(request):
            _fail("INPUT_DIGEST_MISMATCH", "mutation input digest differs", "$.input_digest")

    def _mutation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._check_request(request)
        value = self._load(str(request["workflow_id"]))
        if value["actor"] != request["actor"]:
            _fail("ACTOR_MISMATCH", "workflow actor differs", "$.actor")
        if value["revision"] != request["expected_revision"]:
            _fail("REVISION_CONFLICT", "workflow revision changed", "$.expected_revision")
        return value

    def _pin_batch(self, batch_id: str, actor: str) -> dict[str, Any]:
        batch = self.knowledge._batch(batch_id)
        if batch["actor"] != actor:
            _fail("ACTOR_MISMATCH", "batch actor differs")
        return {"batch_id": batch_id,
                "document_registry_revision": batch["document_registry_revision"],
                "task_ids_fingerprint": "sha256:" + fingerprint(batch["task_ids"])}

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._check_request(request)
        if request["expected_revision"] != 0:
            _fail("REVISION_CONFLICT", "new workflow requires revision 0")
        workflow_id = str(request["workflow_id"])
        path = self._path(workflow_id)
        scope = request["scope"]
        if not isinstance(scope, dict):
            _fail("INVALID_SCHEMA", "scope must be an object")
        batch_id = request.get("batch_id")
        pinned: dict[str, Any] = {}
        if batch_id is not None:
            pinned = self._pin_batch(str(batch_id), str(request["actor"]))
        value = {
            "contract": WORKFLOW_CONTRACT, "workflow_id": workflow_id,
            "schema_version": 1, "actor": request["actor"],
            "profile": request["profile"], "scope": scope,
            "state": "analyzing" if batch_id is not None else "created",
            "current_stage": "analyzing" if batch_id is not None else "created",
            "batch_id": batch_id, "pinned_inputs": pinned,
            "kanban": {"board_id": None, "task_map": []},
            "checkpoints": {name: {"state": "pending", "approval_digest": None,
                                   "approved_by": None} for name in ("checkpoint_1", "checkpoint_2")},
            "artifacts": [], "cancel_requested": False,
            "resume_stage": None, "revision": 1, "template_pins": [],
            "start_digest": request["input_digest"],
        }
        validate_record("ingest_workflow", value)
        with _exclusive_lock(self._lock(workflow_id)):
            if path.exists():
                existing = self._load(workflow_id)
                if existing["start_digest"] == request["input_digest"]:
                    return existing
                _fail("IDEMPOTENCY_CONFLICT", "workflow id already exists with other inputs")
            self._write(value)
        return value

    def status(self, workflow_id: str, compact: bool = False) -> dict[str, Any]:
        value = self._load(workflow_id)
        if not compact:
            return value
        return {key: value[key] for key in ("workflow_id", "revision", "state",
                "current_stage", "batch_id", "profile", "cancel_requested", "checkpoints")}

    def reconcile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            target = request["target_stage"]
            if value["cancel_requested"] or value["state"] in TERMINAL:
                _fail("WORKFLOW_STOPPED", "resume before advancing workflow")
            current = value["current_stage"]
            if current in ("checkpoint_1", "checkpoint_2"):
                if value["checkpoints"][current]["state"] != "approved":
                    _fail("AWAITING_APPROVAL", "checkpoint requires explicit approval")
            next_stage = STAGES[STAGES.index(current) + 1] if current != "completed" else None
            if target not in (next_stage, "partial", "failed") or (target in ("partial", "failed") and current != "validating"):
                _fail("INVALID_TRANSITION", "reconcile advances exactly one stage")
            if target == "analyzing" and value["batch_id"] is None:
                batch_id = str(request.get("batch_id", ""))
                value["pinned_inputs"] = self._pin_batch(batch_id, value["actor"])
                value["batch_id"] = batch_id
            if target in ("analyzing", "reducing", "checkpoint_1", "build_finalizing"):
                if value["batch_id"] is None:
                    _fail("BATCH_REQUIRED", "knowledge stages require a batch")
                batch = self.knowledge._batch(value["batch_id"])
                if "sha256:" + fingerprint(batch["task_ids"]) != value["pinned_inputs"]["task_ids_fingerprint"]:
                    _fail("STALE_INPUT", "batch task set changed")
            if target == "checkpoint_1":
                batch = self.knowledge._batch(value["batch_id"])
                if not batch.get("run_ids") or batch["state"] != "checkpoint_1":
                    _fail("INCOMPLETE_COVERAGE", "validated draft runs required for checkpoint 1")
            if target in ("release_planning", "checkpoint_2", "applying", "indexing",
                          "validating", "completed", "partial", "failed") and not request.get("artifact_refs"):
                _fail("ARTIFACT_REQUIRED", "downstream transition requires artifact references")
            for ref in request.get("artifact_refs", []):
                if not isinstance(ref, str) or not _vault_path(self.vault, ref).is_file():
                    _fail("ARTIFACT_REQUIRED", "referenced Vault artifact is unavailable")
            value["artifacts"].append({"stage": target,
                                       "refs": list(request.get("artifact_refs", []))})
            if target not in ("partial", "failed"):
                value["current_stage"] = target
            value["state"] = target
            if target in ("checkpoint_1", "checkpoint_2"):
                checkpoint = value["checkpoints"][target]
                checkpoint["approval_digest"] = "sha256:" + fingerprint({
                    "workflow_id": value["workflow_id"], "checkpoint": target,
                    "pinned_inputs": value["pinned_inputs"],
                    "artifacts": value["artifacts"], "batch_id": value["batch_id"]})
                checkpoint["state"] = "pending"
            value["revision"] += 1
            self._write(value)
            return value

    def approve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            checkpoint_name = str(request["checkpoint"])
            if value["cancel_requested"] or value["current_stage"] != checkpoint_name:
                _fail("INVALID_TRANSITION", "workflow is not awaiting this checkpoint")
            if checkpoint_name not in ("checkpoint_1", "checkpoint_2"):
                _fail("INVALID_SCHEMA", "invalid checkpoint")
            checkpoint = value["checkpoints"][checkpoint_name]
            if checkpoint["state"] != "pending" or checkpoint["approval_digest"] != request.get("approval_digest"):
                _fail("APPROVAL_DIGEST_MISMATCH", "approval digest is stale or incorrect")
            checkpoint["state"] = "approved"
            checkpoint["approved_by"] = request["actor"]
            value["revision"] += 1
            self._write(value)
            return value

    def cancel(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if value["state"] in TERMINAL:
                _fail("WORKFLOW_STOPPED", "workflow already terminal")
            value["resume_stage"] = value["current_stage"]
            value["state"] = "cancelled"
            value["cancel_requested"] = True
            value["revision"] += 1
            self._write(value)
            return value

    def resume(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if value["state"] not in TERMINAL:
                return value  # recovery reads the authoritative ledger; dispatch is phase 6
            if value["state"] != "cancelled" or not value["cancel_requested"]:
                _fail("INVALID_TRANSITION", "terminal workflow cannot resume")
            value["state"] = value["resume_stage"]
            value["current_stage"] = value["resume_stage"]
            value["resume_stage"] = None
            value["cancel_requested"] = False
            value["revision"] += 1
            self._write(value)
            return value

    def rebuild_kanban(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Persist desired nodes only; stage 6 will materialize a Kanban board."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if value["cancel_requested"]:
                _fail("WORKFLOW_STOPPED", "cancelled workflow cannot schedule")
            task_map: list[dict[str, str]] = []
            if value["batch_id"]:
                for item in self.knowledge._slices(value["batch_id"]):
                    if item["state"] != "completed":
                        key = f"pass-slice:{item['slice_id']}"
                        task_map.append({"node": key, "idempotency_key":
                            "ingest:" + value["workflow_id"] + ":" + key + ":" + item["input_fingerprint"]})
            existing = {item["node"]: item for item in value["kanban"]["task_map"]}
            for item in task_map:
                prior = existing.get(item["node"])
                if prior and prior["idempotency_key"] == item["idempotency_key"]:
                    item.update({k: prior[k] for k in ("task_id",) if k in prior})
            task_map.extend(item for item in value["kanban"]["task_map"]
                            if not item["node"].startswith("pass-slice:"))
            value["kanban"] = {"board_id": value["kanban"]["board_id"], "task_map": task_map}
            value["revision"] += 1
            self._write(value)
            return value

    def bind_kanban(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Record replaceable Kanban identities after idempotent CLI creation."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if value["cancel_requested"]:
                _fail("WORKFLOW_STOPPED", "cancelled workflow cannot bind Kanban")
            board_id = str(request["board_id"])
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", board_id):
                _fail("INVALID_SCHEMA", "invalid Kanban board slug")
            if value["kanban"]["board_id"] not in (None, board_id):
                _fail("IDEMPOTENCY_CONFLICT", "workflow already bound to another board")
            task_map = list(request["task_map"])
            nodes = [item["node"] for item in task_map]
            if len(nodes) != len(set(nodes)) or any(
                    not item["idempotency_key"].startswith(
                        f"ingest:{value['workflow_id']}:") for item in task_map):
                _fail("INVALID_SCHEMA", "invalid workflow Kanban task map")
            value["kanban"] = {"board_id": board_id, "task_map": task_map}
            value["revision"] += 1
            self._write(value)
            return value

    def pin_templates(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Snapshot a complete immutable worker pack before Kanban activation."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            raw = list(request["templates"])
            kinds = [item["kind"] for item in raw]
            if sorted(kinds) != sorted(WORKER_KINDS):
                _fail("INVALID_SCHEMA", "worker template pack must contain each kind once")
            pins = []
            payloads = []
            for item in sorted(raw, key=lambda part: part["kind"]):
                kind, template_id, content = item["kind"], item["template_id"], item["content"]
                if (not isinstance(template_id, str) or not template_id.strip()
                        or not isinstance(content, str) or not content.strip()):
                    _fail("INVALID_SCHEMA", "worker template content and id are required")
                data = content.encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                path = f"{WORKFLOW_ROOT}/{value['workflow_id']}/templates/{kind}-{digest}.md"
                pins.append({"kind": kind, "template_id": template_id,
                             "template_hash": f"sha256:{digest}", "path": path})
                payloads.append((path, data))
            if value.get("template_pins"):
                if value["template_pins"] != pins:
                    _fail("IDEMPOTENCY_CONFLICT", "workflow templates are already pinned")
                self.pinned_templates(value)
                return value
            for path, data in payloads:
                destination = _vault_path(self.vault, path)
                if destination.exists() and destination.read_bytes() != data:
                    _fail("IDEMPOTENCY_CONFLICT", "template snapshot path has other content")
                if not destination.exists():
                    _write_atomic(destination, data)
            value["template_pins"] = pins
            value["revision"] += 1
            self._write(value)
            return value

    def pinned_templates(self, value: Mapping[str, Any]) -> dict[str, str]:
        """Verify every pinned byte before returning content for worker cards."""
        pins = value.get("template_pins", [])
        if sorted(item["kind"] for item in pins) != sorted(WORKER_KINDS):
            _fail("TEMPLATE_UNAVAILABLE", "workflow lacks a complete worker template pack")
        result = {}
        prefix = f"{WORKFLOW_ROOT}/{value['workflow_id']}/templates/"
        for item in pins:
            path = item["path"]
            if not path.startswith(prefix):
                _fail("TEMPLATE_UNAVAILABLE", "template snapshot is outside workflow")
            try:
                data = _vault_path(self.vault, path).read_bytes()
            except OSError as exc:
                _fail("TEMPLATE_UNAVAILABLE", f"pinned worker template is unavailable: {exc}")
            if "sha256:" + hashlib.sha256(data).hexdigest() != item["template_hash"]:
                _fail("TEMPLATE_CHANGED", "pinned worker template content changed")
            try:
                result[item["kind"]] = data.decode("utf-8")
            except UnicodeError:
                _fail("TEMPLATE_CHANGED", "pinned worker template is not UTF-8")
        return result
