"""Vault-authoritative ingest workflow ledger; dispatch is a separate adapter."""
from __future__ import annotations

import hashlib
import copy
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .knowledge_build import FileKnowledgeBuildService
from .source_units import FileSourceUnitService
from .vault_finalize import FileVaultFinalizeService
from .source_units import (_exclusive_lock, _json_bytes, _load_json,
                           _vault_path, _write_atomic)
from .validation import ContractError, fingerprint, validate_record

WORKFLOW_ROOT = "_system/ledgers/ingest-workflows"
WORKFLOW_CONTRACT = "hermes-ingest-workflow/v1"
SOURCE_FAILURE_CODES = {"PDF_UNREADABLE", "CONVERSION_FAILED", "UNSUPPORTED_FORMAT",
                        "BUNDLE_VALIDATION_FAILED", "SOURCE_UNIT_VALIDATION_FAILED"}
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
DISPLAY_PHASES = {
    "compact-3": (
        ("ingest_to_build_checkpoint", ("created", "source_preparing", "planning",
                                        "analyzing", "reducing", "checkpoint_1")),
        ("build_to_release_checkpoint", ("build_finalizing", "release_planning",
                                           "checkpoint_2")),
        ("release_and_acceptance", ("applying", "indexing", "validating",
                                    "completed")),
    ),
    "diagnostic-6": (
        ("source_preparation", ("created", "source_preparing")),
        ("exact_plan", ("planning",)),
        ("analysis_and_checkpoint_1", ("analyzing", "reducing", "checkpoint_1")),
        ("build_finalize", ("build_finalizing",)),
        ("release_plan_and_checkpoint_2", ("release_planning", "checkpoint_2")),
        ("release_and_acceptance", ("applying", "indexing", "validating",
                                    "completed")),
    ),
}
_ID = re.compile(r"^ingest-[A-Za-z0-9][A-Za-z0-9_.:-]{0,152}$")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


def mutation_digest(request: Mapping[str, Any]) -> str:
    """Digest of the complete mutation request except its digest field."""
    return "sha256:" + fingerprint({k: v for k, v in request.items()
                                    if k != "input_digest"})


def display_phase(value: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only UI projection; never an input to transitions or the worker DAG."""
    profile, stage = value["profile"], value["current_stage"]
    for number, (name, stages) in enumerate(DISPLAY_PHASES[profile], 1):
        if stage in stages:
            checkpoint = (stage if stage in ("checkpoint_1", "checkpoint_2")
                          and value["checkpoints"][stage]["state"] != "approved" else None)
            return {"phase": number, "phase_count": len(DISPLAY_PHASES[profile]),
                    "name": name, "awaiting_approval": checkpoint}
    _fail("INVALID_SCHEMA", "workflow stage has no display phase")


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
        if batch_id is not None and scope.get("source_paths"):
            _fail("INVALID_SCHEMA", "adopted batches cannot bypass scoped source preparation")
        pinned: dict[str, Any] = {}
        if batch_id is not None:
            pinned = self._pin_batch(str(batch_id), str(request["actor"]))
        value = {
            "contract": WORKFLOW_CONTRACT, "workflow_id": workflow_id,
            "schema_version": 1, "actor": request["actor"],
            "profile": request["profile"], "scope": scope,
            "state": "analyzing" if batch_id is not None else "created",
            "current_stage": "analyzing" if batch_id is not None else "created",
            "batch_id": batch_id, "release_id": None, "release_plan_id": None,
            "pinned_inputs": pinned,
            "dispatch_policy": {"mode": "disabled", "slice_ids": [],
                                "selection_digest": None},
            "kanban": {"board_id": None, "task_map": []},
            "checkpoints": {name: {"state": "pending", "approval_digest": None,
                                   "approved_by": None, "decision_ref": None,
                                   "decision_sha256": None}
                            for name in ("checkpoint_1", "checkpoint_2")},
            "artifacts": [], "source_outcomes": [], "cancel_requested": False,
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
        result = {key: value[key] for key in ("workflow_id", "revision", "state",
                  "current_stage", "batch_id", "profile", "cancel_requested", "checkpoints")}
        result["release_id"] = value.get("release_id")
        result["display"] = display_phase(value)
        result["dispatch_policy"] = value.get("dispatch_policy", {
            "mode": "disabled", "slice_ids": [], "selection_digest": None})
        result["source_coverage"] = self.source_coverage(value)
        return result

    @staticmethod
    def source_coverage(value: Mapping[str, Any]) -> dict[str, Any]:
        paths = value["scope"]["source_paths"]
        outcomes = {item["path"]: item for item in value.get("source_outcomes", [])}
        return {"requested": len(paths),
                "ready": [path for path in paths if outcomes.get(path, {}).get("status") == "ready"],
                "failed": [path for path in paths if outcomes.get(path, {}).get("status") == "failed"],
                "gaps": [{"path": path, "content_sha256": outcomes[path]["content_sha256"],
                          "error_code": outcomes[path]["error_code"], "reason": outcomes[path]["reason"]}
                         for path in paths if outcomes.get(path, {}).get("status") == "failed"],
                "pending": [path for path in paths if path not in outcomes]}

    def record_source_outcome(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Commit one source result; a failed source is a terminal coverage gap, not a batch abort."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            path = str(request["path"])
            if path not in value["scope"]["source_paths"]:
                _fail("ACCESS_DENIED", "source is outside workflow scope")
            source = _vault_path(self.vault, path)
            if not source.is_file():
                _fail("SOURCE_UNAVAILABLE", "source file is missing")
            content_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            if request.get("content_sha256") != content_sha:
                _fail("SOURCE_CHANGED", "source content hash changed")
            status = request.get("status")
            if status not in ("ready", "failed"):
                _fail("INVALID_SCHEMA", "source status must be ready or failed")
            outcome = {"path": path, "content_sha256": content_sha, "status": status,
                       "resource_id": None, "unit_set_id": None, "error_code": None,
                       "reason": None, "artifact_refs": []}
            if status == "ready":
                resource_id, unit_set_id = str(request.get("resource_id", "")), str(request.get("unit_set_id", ""))
                source_units = FileSourceUnitService(self.vault)
                checked = source_units.validate(resource_id, unit_set_id)
                current = source_units._current(resource_id)
                _, units, _ = source_units._load_set(resource_id, unit_set_id)
                if (checked["unit_set_id"] != unit_set_id or current is None or
                        current["unit_set_id"] != unit_set_id or not units or
                        any(unit["source_sha256"] != content_sha for unit in units)):
                    _fail("STALE_INPUT", "ready source must name its current validated UnitSet")
                outcome.update(resource_id=resource_id, unit_set_id=unit_set_id)
            else:
                code, reason = str(request.get("error_code", "")).strip(), str(request.get("reason", "")).strip()
                if not code or not reason:
                    _fail("INVALID_SCHEMA", "failed source requires an error code and reason")
                if code not in SOURCE_FAILURE_CODES:
                    _fail("INVALID_SOURCE_FAILURE", "worker/runtime errors must block the card, not mark the source failed")
                outcome.update(error_code=code, reason=reason)
            refs = list(request.get("artifact_refs", []))
            if any(not isinstance(ref, str) or not _vault_path(self.vault, ref).is_file() for ref in refs):
                _fail("ARTIFACT_REQUIRED", "source outcome references must exist in the Vault")
            outcome["artifact_refs"] = refs
            existing = {item["path"]: item for item in value.get("source_outcomes", [])}
            if path in existing:
                if existing[path] == outcome:
                    return value
                _fail("IDEMPOTENCY_CONFLICT", "source outcome is already recorded")
            if value["cancel_requested"] or value["batch_id"] is not None or value["current_stage"] not in ("created", "source_preparing"):
                _fail("INVALID_TRANSITION", "source outcomes require active source preparation")
            existing[path] = outcome
            value["source_outcomes"] = [existing[p] for p in value["scope"]["source_paths"] if p in existing]
            coverage = self.source_coverage(value)
            value["current_stage"] = "planning" if not coverage["pending"] and coverage["ready"] else "source_preparing"
            value["state"] = value["current_stage"]
            value["revision"] += 1
            self._write(value)
            return value

    def repair_preparation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Operator-only, auditable replacement of pre-batch contracts and erroneous outcomes."""
        self._check_request(request)
        if os.environ.get("HERMES_KANBAN_TASK"):
            _fail("ACCESS_DENIED", "workers cannot repair their own workflow contracts")
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            existing = self._load(str(request["workflow_id"]))
            repair_id = str(request["repair_id"])
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", repair_id):
                _fail("INVALID_SCHEMA", "invalid repair id")
            for prior in existing.get("repair_history", []):
                if prior["repair_id"] == repair_id:
                    if prior["input_digest"] != request["input_digest"]:
                        _fail("IDEMPOTENCY_CONFLICT", "repair id already used")
                    return existing
            value = self._mutation(request)
            if not value["cancel_requested"] or value["batch_id"] is not None:
                _fail("INVALID_TRANSITION", "repair requires cancelled pre-batch workflow")
            reason = str(request.get("reason", "")).strip()
            refs = list(request.get("evidence_refs", []))
            if not reason or not refs or any(not _vault_path(self.vault, ref).is_file() for ref in refs):
                _fail("ARTIFACT_REQUIRED", "repair requires a reason and existing evidence")
            mode = request.get("execution_mode", value["scope"].get("execution_mode", "manual"))
            if mode not in ("manual", "canary_only", "auto_full"):
                _fail("INVALID_SCHEMA", "invalid execution mode")
            reset = {item["path"]: item["outcome_digest"] for item in request.get("reset_sources", [])}
            outcomes = {item["path"]: item for item in value.get("source_outcomes", [])}
            for path, digest in reset.items():
                outcome = outcomes.get(path)
                if outcome is None or outcome["status"] != "failed" or "sha256:" + fingerprint(outcome) != digest:
                    _fail("STALE_INPUT", "repair must pin an existing failed outcome")
                if hashlib.sha256(_vault_path(self.vault, path).read_bytes()).hexdigest() != outcome["content_sha256"]:
                    _fail("SOURCE_CHANGED", "repair cannot substitute changed source bytes")
            templates = request["templates"]
            if sorted(item["kind"] for item in templates) != sorted(WORKER_KINDS):
                _fail("INVALID_SCHEMA", "repair requires a complete worker pack")
            before_ref = f"{WORKFLOW_ROOT}/{value['workflow_id']}/repairs/{repair_id}/before.json"
            before = _json_bytes(value)
            before_path = _vault_path(self.vault, before_ref)
            if before_path.exists() and before_path.read_bytes() != before:
                _fail("IDEMPOTENCY_CONFLICT", "repair snapshot differs")
            pins = []
            for item in sorted(templates, key=lambda item: item["kind"]):
                if not item["content"].strip() or not item["template_id"].strip():
                    _fail("INVALID_SCHEMA", "empty worker template")
                data = item["content"].encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                ref = f"{WORKFLOW_ROOT}/{value['workflow_id']}/templates/{item['kind']}-{digest}.md"
                dest = _vault_path(self.vault, ref)
                if dest.exists() and dest.read_bytes() != data:
                    _fail("TEMPLATE_CHANGED", "template snapshot differs")
                if not dest.exists():
                    _write_atomic(dest, data)
                pins.append({"kind": item["kind"], "template_id": item["template_id"],
                             "template_hash": "sha256:" + digest, "path": ref})
            if not before_path.exists():
                _write_atomic(before_path, before)
            value = copy.deepcopy(value)
            value["source_outcomes"] = [item for item in value.get("source_outcomes", []) if item["path"] not in reset]
            value["template_pins"] = pins
            value["scope"]["execution_mode"] = mode
            value["kanban"]["task_map"] = []  # old workers must fail closed after migration
            value["resume_stage"] = "source_preparing"
            value.setdefault("repair_history", []).append({"repair_id": repair_id,
                "input_digest": request["input_digest"], "before_ref": before_ref,
                "before_sha256": hashlib.sha256(before).hexdigest(), "reason": reason,
                "actor": value["actor"], "evidence_refs": refs})
            value["revision"] += 1
            self._write(value)
            return value

    def bind_release_plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Bind one validated, blocker-free plan to the workflow before checkpoint 2."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if value["cancel_requested"] or value["current_stage"] != "release_planning":
                _fail("INVALID_TRANSITION", "release plan binding requires release_planning")
            release_id = str(request["release_id"])
            service = FileVaultFinalizeService(self.vault)
            plan = _load_json(service._plan_path(release_id))
            validate_record("vault_finalize_plan", plan)
            if (plan["actor"] != value["actor"] or plan["plan_id"] != request.get("plan_id")
                    or plan["state"] != "draft" or plan["blockers"]):
                _fail("STALE_INPUT", "release plan identity, actor, state or blockers changed")
            batch = self.knowledge._batch(value["batch_id"])
            if ({item["run_id"] for item in plan["build_runs"]} != set(batch["run_ids"])
                    or not batch["run_ids"] or batch["state"] != "completed"):
                _fail("INCOMPLETE_COVERAGE", "release plan must contain all completed batch runs")
            for item in plan["build_runs"]:
                checked = self.knowledge.validate_run(item["run_id"])
                if checked["state"] != "completed" or checked["revision"] != item["revision"]:
                    _fail("STALE_INPUT", "release plan run revision changed")
            if value.get("release_id") is not None:
                if (value["release_id"] == release_id and value["release_plan_id"] == plan["plan_id"]):
                    return value
                _fail("IDEMPOTENCY_CONFLICT", "workflow already binds another release plan")
            updated = dict(value)
            updated["release_id"] = release_id
            updated["release_plan_id"] = plan["plan_id"]
            updated["revision"] += 1
            self._write(updated)
            return updated

    def canary_preview(self, workflow_id: str, limit: int = 8) -> dict[str, Any]:
        """Read-only deterministic selection; no lease or worker activation."""
        if type(limit) is not int or not 1 <= limit <= 8:
            _fail("INVALID_SCHEMA", "canary limit must be between 1 and 8")
        value = self.status(workflow_id)
        if value["cancel_requested"] or value["current_stage"] != "analyzing":
            _fail("WORKFLOW_STOPPED", "canary requires an active analyzing workflow")
        if value["batch_id"] is None:
            _fail("BATCH_REQUIRED", "canary requires an adopted batch")
        self.pinned_templates(value)
        batch = self.knowledge._batch(value["batch_id"])
        if (batch["actor"] != value["actor"] or batch.get("cancel_requested")
                or batch["document_registry_revision"] !=
                value["pinned_inputs"].get("document_registry_revision")
                or "sha256:" + fingerprint(batch["task_ids"]) !=
                value["pinned_inputs"].get("task_ids_fingerprint")):
            _fail("STALE_INPUT", "adopted batch identity or task set changed")
        if not batch.get("slices_initialized"):
            _fail("CANARY_UNAVAILABLE", "initialize slices before canary selection")
        existing = value.get("dispatch_policy", {})
        if existing.get("slice_ids"):
            selected = [self.knowledge._slice(value["batch_id"], slice_id)
                        for slice_id in existing["slice_ids"]]
            digest = existing["selection_digest"]
        else:
            ready = [item for item in self.knowledge._slices(value["batch_id"])
                     if item["state"] == "ready"]
            if len(ready) < limit:
                _fail("CANARY_UNAVAILABLE", "fewer ready slices than requested")
            selected = ready[:limit]
            digest = "sha256:" + fingerprint({
                "workflow_id": workflow_id,
                "slice_inputs": [{"slice_id": item["slice_id"],
                                  "input_fingerprint": item["input_fingerprint"]}
                                 for item in selected],
            })
        return {"workflow_id": workflow_id, "batch_id": value["batch_id"],
                "revision": value["revision"], "mode": existing.get("mode", "disabled"),
                "selection_digest": digest,
                "slices": [{"slice_id": item["slice_id"], "state": item["state"],
                            "task_ids": item["task_ids"],
                            "input_fingerprint": item["input_fingerprint"]}
                           for item in selected]}

    def arm_canary(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a one-time allowlist; never widen an existing canary."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            requested = list(request["slice_ids"])
            if len(requested) != len(set(requested)) or not 1 <= len(requested) <= 8:
                _fail("INVALID_SCHEMA", "canary requires 1 to 8 distinct slice IDs")
            preview = self.canary_preview(value["workflow_id"], len(requested))
            selected = [item["slice_id"] for item in preview["slices"]]
            if (requested != selected or request.get("selection_digest") !=
                    preview["selection_digest"]):
                _fail("STALE_INPUT", "canary selection changed since preview")
            policy = value.get("dispatch_policy", {"mode": "disabled", "slice_ids": [],
                                                   "selection_digest": None})
            if policy["mode"] == "canary":
                return value
            if policy["mode"] != "disabled":
                _fail("INVALID_TRANSITION", "unsupported dispatch mode")
            updated = dict(value)
            updated["dispatch_policy"] = {"mode": "canary", "slice_ids": selected,
                                          "selection_digest": preview["selection_digest"]}
            updated["revision"] += 1
            self._write(updated)
            return updated

    def disarm_canary(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            policy = value.get("dispatch_policy", {"mode": "disabled", "slice_ids": [],
                                                   "selection_digest": None})
            if policy["mode"] == "disabled":
                return value
            if policy["mode"] != "canary":
                _fail("INVALID_TRANSITION", "unsupported dispatch mode")
            updated = dict(value)
            updated["dispatch_policy"] = {**policy, "mode": "disabled"}
            updated["revision"] += 1
            self._write(updated)
            return updated

    def promote_canary(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Open remaining Pass slices only after the pinned eight-slice trial passed."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            if (value["scope"].get("execution_mode") != "auto_full"
                    or value["current_stage"] != "analyzing" or value["cancel_requested"]):
                _fail("INVALID_TRANSITION", "automatic promotion requires an active analyzing workflow")
            policy = value["dispatch_policy"]
            if policy["mode"] == "full":
                return value
            if policy["mode"] != "canary" or len(policy["slice_ids"]) != 8:
                _fail("CANARY_UNAVAILABLE", "exactly eight pinned canary slices are required")
            for slice_id in policy["slice_ids"]:
                item = self.knowledge._slice(value["batch_id"], slice_id)
                if item["state"] != "completed" or not item["result_refs"]:
                    _fail("INCOMPLETE_COVERAGE", "canary slice lacks durable Pass coverage")
                for ref in item["result_refs"]:
                    validate_record("knowledge_pass", _load_json(_vault_path(self.vault, ref)))
            updated = dict(value)
            updated["dispatch_policy"] = {**policy, "mode": "full"}
            updated["revision"] += 1
            self._write(updated)
            return updated

    def reconcile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            target = request["target_stage"]
            if value["cancel_requested"] or value["state"] in TERMINAL:
                _fail("WORKFLOW_STOPPED", "resume before advancing workflow")
            current = value["current_stage"]
            if current in ("checkpoint_1", "checkpoint_2"):
                checkpoint = value["checkpoints"][current]
                if checkpoint["state"] != "approved":
                    _fail("AWAITING_APPROVAL", "checkpoint requires a recorded passing decision")
                if checkpoint.get("decision_ref"):
                    decision_path = _vault_path(self.vault, checkpoint["decision_ref"])
                    observed = hashlib.sha256(decision_path.read_bytes()).hexdigest()
                    if observed != checkpoint.get("decision_sha256"):
                        _fail("SOURCE_CHANGED", "checkpoint decision evidence changed")
                    if current == "checkpoint_2":
                        decision = _load_json(decision_path)
                        lint_ref = decision.get("evidence", {}).get("lint_ref")
                        if not lint_ref or _load_json(_vault_path(self.vault, lint_ref)) != decision.get("lint"):
                            _fail("SOURCE_CHANGED", "checkpoint 2 lint evidence changed")
            next_stage = STAGES[STAGES.index(current) + 1] if current != "completed" else None
            if target not in (next_stage, "partial", "failed") or (target in ("partial", "failed") and current != "validating"):
                _fail("INVALID_TRANSITION", "reconcile advances exactly one stage")
            if target == "planning" and value["scope"]["source_paths"]:
                coverage = self.source_coverage(value)
                if coverage["pending"] or not coverage["ready"]:
                    _fail("INCOMPLETE_COVERAGE", "planning requires every source outcome and at least one ready source")
            if target == "completed" and self.source_coverage(value)["failed"]:
                _fail("INCOMPLETE_COVERAGE", "record final workflow as partial while source gaps remain")
            if target == "reducing" and value["scope"].get("execution_mode") == "canary_only":
                _fail("INVALID_TRANSITION", "canary-only workflow stops after its eight Pass slices")
            if target == "reducing" and value["scope"].get("execution_mode") == "auto_full":
                if (value["dispatch_policy"]["mode"] != "full" or
                        any(item["state"] != "completed"
                            for item in self.knowledge._slices(value["batch_id"]))):
                    _fail("INCOMPLETE_COVERAGE", "all Pass slices and canary promotion are required")
            if target == "analyzing" and value["batch_id"] is None:
                batch_id = str(request.get("batch_id", ""))
                coverage = self.source_coverage(value)
                if value["scope"]["source_paths"] and (coverage["pending"] or not coverage["ready"]):
                    _fail("INCOMPLETE_COVERAGE", "all scoped sources need outcomes and at least one ready source")
                source_units = FileSourceUnitService(self.vault)
                for outcome in value.get("source_outcomes", []):
                    path = _vault_path(self.vault, outcome["path"])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != outcome["content_sha256"]:
                        _fail("SOURCE_CHANGED", "source bytes changed after preparation")
                    if outcome["status"] == "ready":
                        current_set = source_units._current(outcome["resource_id"])
                        if current_set is None or current_set["unit_set_id"] != outcome["unit_set_id"]:
                            _fail("STALE_INPUT", "ready source UnitSet is no longer current")
                        source_units.validate(outcome["resource_id"], outcome["unit_set_id"])
                ready = {item["resource_id"]: item["unit_set_id"]
                         for item in value["source_outcomes"] if item["status"] == "ready"}
                if len(ready) != len(coverage["ready"]):
                    _fail("INCOMPLETE_COVERAGE", "ready sources cannot share a resource identity")
                batch = self.knowledge._batch(batch_id)
                observed = {(ref["unit_ref"]["resource_id"], ref["unit_ref"]["unit_set_id"])
                            for task_id in batch["task_ids"]
                            for ref in self.knowledge._task(task_id)["target_refs"]}
                expected = set(ready.items())
                if ready and ({resource for resource, _ in observed} != set(ready) or not observed <= expected):
                    _fail("INCOMPLETE_COVERAGE", "batch must cover ready UnitSets and exclude failed sources")
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
            automatic = request.get("decision_mode") == "auto_verified"
            if automatic:
                if value["scope"].get("execution_mode") != "auto_full":
                    _fail("ACCESS_DENIED", "automatic checkpoint decision was not requested")
                decision_ref = str(request.get("decision_ref", ""))
                expected_ref = (f"{WORKFLOW_ROOT}/{value['workflow_id']}/"
                                f"decisions/{checkpoint_name}.json")
                if decision_ref != expected_ref:
                    _fail("ACCESS_DENIED", "checkpoint decision path is outside this workflow")
                decision_path = _vault_path(self.vault, decision_ref)
                decision_bytes = decision_path.read_bytes()
                decision = _load_json(decision_path)
                if (decision.get("contract") != "hermes-ingest-checkpoint-decision/v1"
                        or decision.get("workflow_id") != value["workflow_id"]
                        or decision.get("checkpoint") != checkpoint_name
                        or decision.get("approval_digest") != checkpoint["approval_digest"]
                        or decision.get("ok") is not True or decision.get("blocking_codes") != []):
                    _fail("CHECKPOINT_BLOCKED", "automatic decision report has blocking or stale evidence")
                if checkpoint_name == "checkpoint_1":
                    validated = self.knowledge.validate_batch(value["batch_id"])
                    if not validated["ok"] or validated["batch"]["state"] != "checkpoint_1":
                        _fail("CHECKPOINT_BLOCKED", "batch validation no longer passes")
                else:
                    service = FileVaultFinalizeService(self.vault)
                    plan = _load_json(service._plan_path(value["release_id"]))
                    validate_record("vault_finalize_plan", plan)
                    lint = decision.get("lint", {})
                    evidence = decision.get("evidence", {})
                    lint_ref = (f"{WORKFLOW_ROOT}/{value['workflow_id']}/"
                                "reports/checkpoint-2-lint.json")
                    plan_ref = f"_system/knowledge-releases/{value['release_id']}/plan.json"
                    if (plan["plan_id"] != value["release_plan_id"] or plan["blockers"]
                            or plan["state"] != "draft" or lint.get("ok") is not True
                            or lint.get("summary", {}).get("errors") != 0
                            or evidence.get("lint_ref") != lint_ref
                            or evidence.get("plan_ref") != plan_ref
                            or _load_json(_vault_path(self.vault, lint_ref)) != lint):
                        _fail("CHECKPOINT_BLOCKED", "release plan or Vault lint has blockers")
                    rebuilt = service._build_plan({
                        "release_id": plan["release_id"], "actor": plan["actor"],
                        "expected_state_revision": plan["expected_state_revision"],
                        "build_run_ids": [item["run_id"] for item in plan["build_runs"]],
                        "source_changes": plan["source_changes"], "reason": plan["reason"]})
                    if rebuilt != plan:
                        _fail("STALE_INPUT", "release plan changed after validation")
                checkpoint["decision_ref"] = decision_ref
                checkpoint["decision_sha256"] = hashlib.sha256(decision_bytes).hexdigest()
            checkpoint["state"] = "approved"
            checkpoint["approved_by"] = "hermes:auto" if automatic else request["actor"]
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
