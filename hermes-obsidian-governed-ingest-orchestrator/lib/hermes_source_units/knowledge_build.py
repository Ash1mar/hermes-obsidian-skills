"""P3 file-backed Pass/Reduce knowledge construction and Build Finalize."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .source_units import (FileSourceUnitService, _json_bytes, _load_json,
                           _exclusive_lock, _sha, _vault_path, _write_atomic)
from .validation import (ContractError, canonical_json, fingerprint,
                         validate_record, validate_references)

WORK_ROOT = "_system/ledgers/unit-work"
BATCH_ROOT = "_system/ledgers/knowledge-build-batches"
BUILD_ROOT = "_system/knowledge-builds"
IDENTITY_PATH = "_system/metadata/knowledge-identities.json"
KNOWLEDGE_CONTRACT = "hermes-knowledge-build-run/v1"
READING_MEASUREMENT_CONTRACT = "hermes-reading-window-measurement/v1"
READING_SERIALIZATION = "canonical-json-window-materials/v1"
READING_CONTEXT_SELECTION = "source-context-trimmed-to-serialized-budget/v1"
SLICE_CONTRACT = "hermes-knowledge-build-slice/v1"
DEFAULT_SLICE_CONFIG = {
    "pass_worker_concurrency": 2,
    "slice_max_tasks": 3,
    "slice_max_input_codepoints": 30000,
    "lease_seconds": 1800,
    "heartbeat_seconds": 60,
    "max_attempts": 3,
}
PASS_TEMPLATE_ID = "knowledge-pass/v1"
RESOURCE_REDUCE_TEMPLATE_ID = "knowledge-resource-reduce/v1"
GLOBAL_REDUCE_TEMPLATE_ID = "knowledge-global-reduce/v1"
TRANSIENT_BACKOFF_SECONDS = (60, 180, 600)
DEFAULT_REDUCTION_CONFIG = {"resource_reducer_concurrency": 2,
                            "global_reducer_concurrency": 1}
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SLICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_REDUCTION_ID = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


def _utc(value: datetime | str | None = None) -> tuple[datetime, str]:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("INVALID_SCHEMA", "$.now", "invalid UTC timestamp") from exc
    else:
        moment = value
    if moment.tzinfo is None:
        _fail("INVALID_SCHEMA", "timestamp must carry a timezone", "$.now")
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment, moment.isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("INVALID_SCHEMA", "$", "invalid stored UTC timestamp") from exc
    if moment.tzinfo is None:
        _fail("INVALID_SCHEMA", "stored timestamp must carry a timezone")
    return moment.astimezone(timezone.utc)


def _pass_template_hash() -> str:
    return fingerprint({"template_id": PASS_TEMPLATE_ID,
                        "task_contract": KNOWLEDGE_CONTRACT})


def _reducer_template_hash(template_id: str) -> str:
    return fingerprint({"template_id": template_id,
                        "task_contract": KNOWLEDGE_CONTRACT})


def _failure_class(code: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", code.upper()).strip("_")
    if normalized in {"429", "HTTP_429", "RATE_LIMIT", "RATE_LIMITED", "TOO_MANY_REQUESTS"}:
        return "rate_limited"
    if normalized in {"TIMEOUT", "MODEL_TIMEOUT", "PROCESS_EXIT", "PROCESS_ERROR",
                      "PROCESS_ABNORMAL_EXIT", "WORKER_EXIT"}:
        return "transient"
    if normalized in {"INVALID_MODEL_JSON", "MODEL_JSON_INVALID", "MALFORMED_MODEL_JSON"}:
        return "invalid_model_output"
    if normalized in {"STALE_INPUT", "STALE_PLAN", "REVISION_CONFLICT",
                      "INPUT_HASH_CHANGED", "SOURCE_CHANGED"}:
        return "stale_input"
    if normalized in {"READING_BUDGET_MISMATCH", "READING_WINDOW_OVERSIZE"}:
        return "reading_budget"
    if normalized == "WHOLE_ASSET_OVERSIZE":
        return "whole_asset_oversize"
    if normalized in {"AWAITING_APPROVAL", "CHECKPOINT_REQUIRED", "CHECKPOINT_1",
                      "CHECKPOINT_2"}:
        return "awaiting_approval"
    if normalized in {"INVALID_SCHEMA", "INVALID_PASS", "PROVENANCE_ERROR",
                      "OUTSIDE_READING_PACKAGE", "TASK_WINDOW_MISMATCH",
                      "UNRESOLVED_REFERENCE", "IDENTITY_MISMATCH"}:
        return "contract"
    return "permanent"


def _same_ref(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return canonical_json(left) == canonical_json(right)


def _unique(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for value in values:
        key = canonical_json(value)
        if key not in seen:
            seen.add(key)
            result.append(dict(value))
    return result


def _covers(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    if not _same_ref(outer["unit_ref"], inner["unit_ref"]):
        return False
    wanted, offered = inner.get("span"), outer.get("span")
    if offered is None:
        return True
    if wanted is None:
        return False
    return offered["start"] <= wanted["start"] < wanted["end"] <= offered["end"]


def _overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if not _same_ref(left["unit_ref"], right["unit_ref"]):
        return False
    a, b = left.get("span"), right.get("span")
    if a is None or b is None:
        return True
    return a["start"] < b["end"] and b["start"] < a["end"]


class FileKnowledgeBuildService:
    """One-Vault P3 repository. Semantic judgments arrive as explicit requests."""

    def __init__(self, vault_root: str | Path):
        self.source = FileSourceUnitService(vault_root)
        self.vault = self.source.vault
        capability = self.source.vault_manifest.get("source_units", {}).get("capabilities", {})
        if capability.get("knowledge_build") is not True:
            _fail("CAPABILITY_DISABLED", "Vault does not declare P3 knowledge-build capability")

    def _task_path(self, task_id: str) -> Path:
        return _vault_path(self.vault, f"{WORK_ROOT}/{task_id}.json")

    def _run_path(self, run_id: str) -> Path:
        return _vault_path(self.vault, f"{BUILD_ROOT}/{run_id}/manifest.json")

    def _batch_path(self, batch_id: str) -> Path:
        if not _BATCH_ID.fullmatch(batch_id):
            _fail("INVALID_SCHEMA", "invalid knowledge-build batch id", "$.batch_id")
        return _vault_path(self.vault, f"{BATCH_ROOT}/{batch_id}.json")

    def _batch_lock_path(self, batch_id: str) -> Path:
        if not _BATCH_ID.fullmatch(batch_id):
            _fail("INVALID_SCHEMA", "invalid knowledge-build batch id", "$.batch_id")
        return _vault_path(self.vault, f"{BATCH_ROOT}/.locks/{batch_id}.lock")

    def _slice_root(self, batch_id: str) -> Path:
        self._batch_path(batch_id)
        return _vault_path(self.vault, f"{BATCH_ROOT}/{batch_id}/slices")

    def _slice_path(self, batch_id: str, slice_id: str) -> Path:
        if not _SLICE_ID.fullmatch(slice_id):
            _fail("INVALID_SCHEMA", "invalid knowledge-build slice id", "$.slice_id")
        return self._slice_root(batch_id) / f"{slice_id}.json"

    def _resource_reduction_path(self, batch_id: str, reduction_id: str) -> Path:
        if not _REDUCTION_ID.fullmatch(reduction_id):
            _fail("INVALID_SCHEMA", "invalid resource reduction id", "$.reduction_id")
        return _vault_path(
            self.vault, f"{BATCH_ROOT}/{batch_id}/reductions/resources/{reduction_id}.json")

    def _global_reduction_path(self, batch_id: str, coordination_id: str) -> Path:
        if not _REDUCTION_ID.fullmatch(coordination_id):
            _fail("INVALID_SCHEMA", "invalid global coordination id", "$.coordination_id")
        return _vault_path(
            self.vault, f"{BATCH_ROOT}/{batch_id}/reductions/global/{coordination_id}.json")

    def _global_reduction_lock_path(self, batch_id: str) -> Path:
        return _vault_path(self.vault, f"{BATCH_ROOT}/{batch_id}/reductions/.global.lock")

    def _resource_reduction(self, batch_id: str, reduction_id: str) -> dict[str, Any]:
        value = _load_json(self._resource_reduction_path(batch_id, reduction_id))
        validate_record("resource_reduction", value)
        if value["batch_id"] != batch_id:
            _fail("INVALID_SCHEMA", "resource reduction belongs to a different batch")
        return value

    def _global_reduction(self, batch_id: str, coordination_id: str) -> dict[str, Any]:
        value = _load_json(self._global_reduction_path(batch_id, coordination_id))
        validate_record("global_reduction", value)
        if value["batch_id"] != batch_id:
            _fail("INVALID_SCHEMA", "global reduction belongs to a different batch")
        return value

    def _slice(self, batch_id: str, slice_id: str) -> dict[str, Any]:
        value = _load_json(self._slice_path(batch_id, slice_id))
        validate_record("knowledge_slice", value)
        if value["batch_id"] != batch_id:
            _fail("INVALID_SCHEMA", "slice belongs to a different batch")
        return value

    def _slices(self, batch_id: str) -> list[dict[str, Any]]:
        root = self._slice_root(batch_id)
        values = []
        for path in sorted(root.glob("*.json")) if root.exists() else []:
            value = _load_json(path)
            validate_record("knowledge_slice", value)
            if value["batch_id"] != batch_id:
                _fail("INVALID_SCHEMA", "slice directory contains a foreign batch")
            values.append(value)
        return values

    def _write_slice(self, value: Mapping[str, Any]) -> None:
        validate_record("knowledge_slice", value)
        _write_atomic(self._slice_path(str(value["batch_id"]), str(value["slice_id"])),
                      _json_bytes(value))

    def _task(self, task_id: str) -> dict[str, Any]:
        value = _load_json(self._task_path(task_id))
        validate_record("work", value)
        return value

    def _registry(self) -> dict[str, Any]:
        value = _load_json(_vault_path(self.vault, IDENTITY_PATH))
        validate_record("identity_registry", value)
        return value

    def _batch(self, batch_id: str) -> dict[str, Any]:
        value = _load_json(self._batch_path(batch_id))
        validate_record("knowledge_batch", value)
        return value

    def _store_batch(self, batch: Mapping[str, Any], *, state: str | None = None,
                     operation: str | None = None, run_ids: Iterable[str] | None = None,
                     failures: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        updated = copy.deepcopy(dict(batch))
        updated["revision"] = int(updated["revision"]) + 1
        if state is not None:
            updated["state"] = state
        if operation is not None:
            updated["last_operation"] = operation
        if run_ids is not None:
            updated["run_ids"] = list(dict.fromkeys(str(item) for item in run_ids))
        if failures is not None:
            updated["failures"] = [dict(item) for item in failures]
        validate_record("knowledge_batch", updated)
        _write_atomic(self._batch_path(str(updated["batch_id"])), _json_bytes(updated))
        return updated

    def _live_units(self, refs: Iterable[Mapping[str, Any]], actor: str,
                    registry_revision: int) -> list[dict[str, Any]]:
        refs = list(refs)
        if not refs:
            return []
        return self.source.resolve_many(refs, {"actor": actor, "purpose": "construction",
                                               "registry_revision": registry_revision})

    def _reader_config(self, max_codepoints: int | None = None) -> dict[str, Any]:
        maximum = int(self.source.config["reading"]["max_codepoints"]
                      if max_codepoints is None else max_codepoints)
        if maximum < 1:
            _fail("INVALID_BUDGET", "reading maximum must be positive")
        return {"max_codepoints": maximum, "serialization": READING_SERIALIZATION,
                "context_selection": READING_CONTEXT_SELECTION}

    def _unitset_revisions(self, refs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        revisions: dict[tuple[str, str], dict[str, Any]] = {}
        for source_ref in refs:
            unit_ref = source_ref["unit_ref"]
            key = (str(unit_ref["resource_id"]), str(unit_ref["unit_set_id"]))
            if key in revisions:
                continue
            manifest, _, _ = self.source._load_set(*key)
            revisions[key] = {"resource_id": key[0], "unit_set_id": key[1],
                              "revision": int(manifest["revision"])}
        return [revisions[key] for key in sorted(revisions)]

    @staticmethod
    def _is_ancestor_material(item: Mapping[str, Any], core: Iterable[Mapping[str, Any]]) -> bool:
        heading = tuple(item.get("heading_path", []))
        return bool(heading) and any(
            len(heading) < len(tuple(value.get("heading_path", [])))
            and tuple(value.get("heading_path", []))[:len(heading)] == heading
            for value in core)

    @staticmethod
    def _reading_projection(window: Mapping[str, Any], materials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        return {"window": dict(window), "materials": [dict(item) for item in materials]}

    def _assemble_reading(self, refs: list[Mapping[str, Any]], actor: str,
                          registry_revision: int, reader_config: Mapping[str, Any]) -> dict[str, Any]:
        maximum = int(reader_config["max_codepoints"])
        access = {"actor": actor, "purpose": "construction",
                  "registry_revision": registry_revision}
        core = [self.source.get({"source_ref": ref, "access": access}) for ref in refs]
        core_codepoints = sum(len(item["core_text"] or "") for item in core)
        result = self.source.context({"core_refs": refs, "access": access,
                                     "max_codepoints": max(maximum, core_codepoints)})
        context = list(result["context"])
        omitted = list(result["omitted_refs"])

        def material(role: str, item: Mapping[str, Any]) -> dict[str, Any]:
            selected = item["core_text"]
            return {"role": role, "source_ref": item["source_ref"],
                    "unit_content_sha256": item["content_sha256"],
                    "selected_sha256": _sha(selected.encode("utf-8")) if selected is not None else None,
                    "core_text": selected, "asset_refs": item["asset_refs"],
                    "quality_refs": item["quality_refs"], "section_id": item["section_id"],
                    "heading_path": item["heading_path"]}

        def build() -> tuple[dict[str, Any], list[dict[str, Any]], int]:
            context_refs = [item["source_ref"] for item in context]
            truncated = bool(omitted)
            reason = result["reason"]
            if truncated and not reason:
                reason = "serialized reading-package budget omitted context units"
            elif truncated and "serialized reading-package budget" not in reason:
                reason = reason + "; serialized reading-package budget applied"
            payload = {"core_refs": refs, "context_refs": context_refs,
                       "context_header": " | ".join(dict.fromkeys(
                           " / ".join(item.get("heading_path", []))
                           for item in [*core, *context] if item.get("heading_path"))),
                       "omitted_refs": omitted, "truncated": truncated,
                       "reason": reason, "max_codepoints": maximum}
            window = {"contract": "hermes-reading-window/v1",
                      "window_id": fingerprint(payload), **payload}
            validate_record("reading_window", window)
            materials = [material("core", item) for item in core]
            materials.extend(material("context", item) for item in context)
            serialized = len(canonical_json(self._reading_projection(window, materials)).decode("utf-8"))
            return window, materials, serialized

        window, materials, serialized = build()
        while serialized > maximum and context:
            removed = context.pop()
            omitted.insert(0, removed["source_ref"])
            window, materials, serialized = build()

        ancestor_codepoints = sum(
            len(item["core_text"] or "") for item in context
            if self._is_ancestor_material(item, core))
        context_codepoints = sum(len(item["core_text"] or "") for item in context) - ancestor_codepoints
        reader_config_hash = fingerprint(dict(reader_config))
        unitset_revisions = self._unitset_revisions(refs)
        input_fingerprint = "sha256:" + fingerprint({
            "unit_refs": refs, "registry_revision": registry_revision,
            "unitset_revisions": unitset_revisions,
            "reader_config_hash": reader_config_hash,
        })
        whole_asset = any(item.get("locator", {}).get("precision") == "whole-asset" for item in core)
        measurement = {
            "contract": READING_MEASUREMENT_CONTRACT,
            "core_codepoints": core_codepoints,
            "ancestor_codepoints": ancestor_codepoints,
            "context_codepoints": context_codepoints,
            "metadata_codepoints": serialized - core_codepoints - ancestor_codepoints - context_codepoints,
            "serialized_codepoints": serialized, "limit": maximum,
            "fits": serialized <= maximum, "input_fingerprint": input_fingerprint,
            "reader_config_hash": reader_config_hash, "unitset_revisions": unitset_revisions,
            "blocking_code": (None if serialized <= maximum else
                              "WHOLE_ASSET_OVERSIZE" if whole_asset else "READING_WINDOW_OVERSIZE"),
        }
        validate_record("reading_window_measurement", measurement)
        return {"window": window, "materials": materials, "measurement": measurement,
                "core": core, "context": context}

    def measure_reading_window(self, unit_refs: Iterable[Mapping[str, Any]],
                               registry_revision: int,
                               unitset_revisions: Iterable[Mapping[str, Any]] | None,
                               reader_config: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        """Measure the exact canonical reading projection without writing artifacts."""
        refs = [dict(item) for item in unit_refs]
        if not refs:
            _fail("INVALID_SCHEMA", "reading measurement requires at least one UnitRef")
        observed = self._unitset_revisions(refs)
        if unitset_revisions is not None and list(unitset_revisions) != observed:
            _fail("STALE_PLAN", "pinned UnitSet revisions changed during reading measurement")
        return self._assemble_reading(refs, actor, registry_revision, reader_config)["measurement"]

    def measure_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Preview exact per-task reading budgets without creating tasks or a batch."""
        self.source.enable_session_cache()
        actor = str(request["actor"])
        registry_revision = int(request["registry_revision"])
        reader_config = self._reader_config(request.get("max_codepoints"))
        tasks = list(request["tasks"])
        if not tasks:
            _fail("INVALID_SCHEMA", "batch measurement requires at least one task", "$.tasks")
        task_ids = [str(item["task_id"]) for item in tasks]
        if len(set(task_ids)) != len(task_ids):
            _fail("DUPLICATE", "batch measurement contains duplicate task ids", "$.tasks")
        results = []
        for raw in tasks:
            refs = list(raw["target_refs"])
            if not refs:
                _fail("INVALID_SCHEMA", "measured task target_refs cannot be empty", "$.tasks")
            measurement = self.measure_reading_window(
                refs, registry_revision, self._unitset_revisions(refs), reader_config, actor=actor)
            results.append({"task_id": str(raw["task_id"]), "measurement": measurement})
        return {"ok": all(item["measurement"]["fits"] for item in results),
                "reader_config": reader_config,
                "reader_config_hash": fingerprint(reader_config), "results": results}

    @staticmethod
    def _ref_key(source_ref: Mapping[str, Any]) -> bytes:
        return canonical_json(source_ref["unit_ref"])

    def _task_ref_index(self, statuses: set[str], excluded: set[str] | None = None) -> dict[bytes, set[str]]:
        excluded = excluded or set()
        index: dict[bytes, set[str]] = {}
        root = _vault_path(self.vault, WORK_ROOT)
        for path in sorted(root.glob("*.json")) if root.exists() else []:
            task = _load_json(path)
            task_id = task.get("task_id")
            if task_id in excluded or task.get("status") not in statuses:
                continue
            for ref in task.get("target_refs", []):
                index.setdefault(self._ref_key(ref), set()).add(str(task_id))
        return index

    def plan_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        task_id, actor = str(request["task_id"]), str(request["actor"])
        refs = list(request["target_refs"])
        expected = int(request.get("expected_revision", 0))
        path = self._task_path(task_id)
        if path.exists():
            current = self._task(task_id)
            if current["target_refs"] == refs and current["task_type"] == "knowledge_build":
                return {"ok": True, "created": False, "task": current, "overlapping_tasks": []}
            _fail("DUPLICATE", "task_id already identifies different work", "$.task_id")
        if expected != 0:
            _fail("REVISION_CONFLICT", "new task requires expected_revision 0", "$.expected_revision")
        registry_revision = int(request["registry_revision"])
        units = self._live_units(refs, actor, registry_revision)
        task = {"contract": "hermes-unit-work-ledger/v1", "task_id": task_id,
                "task_type": "knowledge_build", "task_contract": KNOWLEDGE_CONTRACT,
                "revision": 1, "attempt": 1, "actor": actor, "status": "pending",
                "target_refs": refs, "inspections": [], "deferred": [], "outputs": [], "reason": ""}
        validate_references("work", task, units)
        overlaps = []
        root = _vault_path(self.vault, WORK_ROOT)
        for other_path in sorted(root.glob("*.json")) if root.exists() else []:
            other = _load_json(other_path)
            if other.get("status") in ("pending", "running") and any(
                    _same_ref(a["unit_ref"], b["unit_ref"])
                    for a in refs for b in other.get("target_refs", [])):
                overlaps.append(other.get("task_id"))
        _write_atomic(path, _json_bytes(task))
        return {"ok": True, "created": True, "task": task,
                "overlapping_tasks": sorted(item for item in overlaps if isinstance(item, str))}

    def _batch_origin(self, request: Mapping[str, Any], default_kind: str) -> dict[str, Any]:
        raw = request.get("origin", {})
        if not isinstance(raw, Mapping):
            _fail("INVALID_SCHEMA", "batch origin must be an object", "$.origin")
        origin = {"kind": str(raw.get("kind", default_kind)),
                  "path": raw.get("path"), "sha256": raw.get("sha256")}
        if origin["path"] is not None:
            path = _vault_path(self.vault, str(origin["path"]))
            if not path.is_file():
                _fail("SOURCE_UNAVAILABLE", "batch origin file does not exist", "$.origin.path")
            observed = _sha(path.read_bytes())
            if origin["sha256"] is not None and origin["sha256"] != observed:
                _fail("SOURCE_CHANGED", "batch origin hash changed", "$.origin.sha256")
            origin["sha256"] = observed
        elif origin["sha256"] is not None:
            _fail("INVALID_SCHEMA", "batch origin hash requires a path", "$.origin.sha256")
        return origin

    def _new_batch(self, batch_id: str, actor: str, registry_revision: int,
                   task_ids: list[str], origin: Mapping[str, Any]) -> dict[str, Any]:
        batch = {"contract": "hermes-knowledge-build-batch/v1", "batch_id": batch_id,
                 "revision": 1, "actor": actor,
                 "document_registry_revision": registry_revision,
                 "origin": dict(origin), "task_ids": task_ids, "run_ids": [],
                 "state": "planned", "last_operation": "plan", "failures": [],
                 "slices_initialized": False, "slice_ids": [], "slice_config": None,
                 "cancel_requested": False, "cooldown_until": None,
                 "cooldown_reason": None,
                 "reduction_config": dict(DEFAULT_REDUCTION_CONFIG),
                 "resource_reduction_ids": [], "global_reduction_id": None}
        validate_record("knowledge_batch", batch)
        return batch

    def plan_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Create a complete task batch with one SourceUnit and overlap preflight."""
        self.source.enable_session_cache()
        batch_id, actor = str(request["batch_id"]), str(request["actor"])
        registry_revision = int(request["registry_revision"])
        exact_reading_budget = bool(request.get("exact_reading_budget", False))
        reader_config = self._reader_config(request.get("max_codepoints"))
        task_requests = list(request["tasks"])
        if not task_requests:
            _fail("INVALID_SCHEMA", "batch plan requires at least one task", "$.tasks")
        task_ids = [str(item["task_id"]) for item in task_requests]
        if len(set(task_ids)) != len(task_ids):
            _fail("DUPLICATE", "batch contains duplicate task ids", "$.tasks")
        all_refs: list[Mapping[str, Any]] = []
        ref_owners: dict[bytes, str] = {}
        for item in task_requests:
            refs = list(item["target_refs"])
            if not refs:
                _fail("INVALID_SCHEMA", "batch task target_refs cannot be empty", "$.tasks")
            for ref in refs:
                key = self._ref_key(ref)
                if key in ref_owners:
                    _fail("DUPLICATE", f"UnitRef is assigned to both {ref_owners[key]} and {item['task_id']}",
                          "$.tasks")
                ref_owners[key] = str(item["task_id"])
            all_refs.extend(refs)
        origin = self._batch_origin(request, "generated")
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch_path = self._batch_path(batch_id)
            if batch_path.exists():
                existing_batch = self._batch(batch_id)
                if (existing_batch["actor"] != actor or existing_batch["task_ids"] != task_ids
                        or existing_batch["document_registry_revision"] != registry_revision):
                    _fail("DUPLICATE", "batch_id already identifies a different task set", "$.batch_id")
                if exact_reading_budget:
                    existing_tasks = [self._task(task_id) for task_id in task_ids]
                    if any(task.get("reader_config_hash") != fingerprint(reader_config)
                           or "reading_measurement" not in task for task in existing_tasks):
                        _fail("STALE_PLAN", "existing batch was not planned with this exact reader configuration")
                    for task in existing_tasks:
                        self._verify_planned_measurement(
                            task, actor, registry_revision, request.get("max_codepoints"))
                return {"ok": True, "created": False, "batch": existing_batch,
                        "created_tasks": 0, "existing_tasks": len(task_ids)}
            units = self._live_units(all_refs, actor, registry_revision)
            active = self._task_ref_index({"pending", "running"}, set(task_ids))
            completed = self._task_ref_index({"completed"}, set(task_ids))
            active_conflicts = sorted({task_id for key in ref_owners for task_id in active.get(key, set())})
            completed_conflicts = sorted({task_id for key in ref_owners for task_id in completed.get(key, set())})
            if active_conflicts:
                _fail("DUPLICATE", "batch overlaps active tasks: " + ", ".join(active_conflicts), "$.tasks")
            if completed_conflicts:
                _fail("DUPLICATE", "batch overlaps completed tasks: " + ", ".join(completed_conflicts), "$.tasks")
            prepared: list[tuple[Path, dict[str, Any], bool]] = []
            for raw in task_requests:
                task_id, refs = str(raw["task_id"]), list(raw["target_refs"])
                if int(raw.get("expected_revision", 0)) != 0:
                    _fail("REVISION_CONFLICT", "new batch task requires expected_revision 0", "$.tasks")
                path = self._task_path(task_id)
                task = {"contract": "hermes-unit-work-ledger/v1", "task_id": task_id,
                        "task_type": "knowledge_build", "task_contract": KNOWLEDGE_CONTRACT,
                        "revision": 1, "attempt": 1, "actor": actor, "status": "pending",
                        "target_refs": refs, "inspections": [], "deferred": [],
                        "outputs": [], "reason": ""}
                if exact_reading_budget:
                    measurement = self.measure_reading_window(
                        refs, registry_revision, self._unitset_revisions(refs),
                        reader_config, actor=actor)
                    if not measurement["fits"]:
                        _fail(str(measurement["blocking_code"]),
                              f"task {task_id} serialized reading window uses "
                              f"{measurement['serialized_codepoints']} of {measurement['limit']} codepoints",
                              "$.tasks")
                    task["reading_measurement"] = measurement
                    task["reader_config_hash"] = measurement["reader_config_hash"]
                validate_references("work", task, units)
                if path.exists():
                    existing = self._task(task_id)
                    if existing != task:
                        _fail("DUPLICATE", "task_id already identifies different work", "$.tasks")
                    prepared.append((path, existing, False))
                else:
                    prepared.append((path, task, True))
            batch = self._new_batch(batch_id, actor, registry_revision, task_ids, origin)
            for path, task, created in prepared:
                if created:
                    _write_atomic(path, _json_bytes(task))
            _write_atomic(batch_path, _json_bytes(batch))
            return {"ok": True, "created": True, "batch": batch,
                    "created_tasks": sum(created for _, _, created in prepared),
                    "existing_tasks": sum(not created for _, _, created in prepared)}

    def adopt_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Attach an existing immutable task plan without rewriting its artifacts."""
        self.source.enable_session_cache()
        batch_id, actor = str(request["batch_id"]), str(request["actor"])
        registry_revision = int(request["registry_revision"])
        task_ids = [str(item) for item in request["task_ids"]]
        if not task_ids or len(set(task_ids)) != len(task_ids):
            _fail("INVALID_SCHEMA", "adopt requires unique task ids", "$.task_ids")
        origin = self._batch_origin(request, "adopted")
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            tasks = [self._task(task_id) for task_id in task_ids]
            if any(task["task_type"] != "knowledge_build" for task in tasks):
                _fail("TASK_CONTRACT_MISMATCH", "batch can adopt only knowledge-build tasks")
            if any(task["actor"] != actor for task in tasks):
                _fail("ACCESS_DENIED", "adopt actor must match every task actor")
            all_refs = [ref for task in tasks for ref in task["target_refs"]]
            keys = [self._ref_key(ref) for ref in all_refs]
            if len(set(keys)) != len(keys):
                _fail("DUPLICATE", "adopted tasks contain duplicate core UnitRefs", "$.task_ids")
            active = self._task_ref_index({"pending", "running"}, set(task_ids))
            completed = self._task_ref_index({"completed"}, set(task_ids))
            external = sorted({task_id for key in keys for task_id in active.get(key, set())
                               | completed.get(key, set())})
            if external:
                _fail("DUPLICATE", "adopted tasks overlap external active/completed tasks: "
                      + ", ".join(external), "$.task_ids")
            self._live_units(all_refs, actor, registry_revision)
            path = self._batch_path(batch_id)
            batch = self._new_batch(batch_id, actor, registry_revision, task_ids, origin)
            batch["last_operation"] = "adopt"
            if path.exists():
                existing = self._batch(batch_id)
                if existing["actor"] != actor or existing["task_ids"] != task_ids:
                    _fail("DUPLICATE", "batch_id already identifies a different task set", "$.batch_id")
                return {"ok": True, "created": False, "batch": existing}
            _write_atomic(path, _json_bytes(batch))
            return {"ok": True, "created": True, "batch": batch}

    @staticmethod
    def _failure(stage: str, item_id: str, exc: Exception) -> dict[str, str]:
        return {"stage": stage, "item_id": item_id,
                "code": exc.code if isinstance(exc, ContractError) else type(exc).__name__,
                "message": str(exc)}

    @staticmethod
    def _slice_config(value: Mapping[str, Any] | None = None) -> dict[str, int]:
        raw = dict(value or {})
        config = {key: int(raw.get(key, default)) for key, default in DEFAULT_SLICE_CONFIG.items()}
        validate_record("knowledge_slice_config", config)
        return config

    def _slice_task_input(self, batch: Mapping[str, Any], task: Mapping[str, Any], *,
                          remeasure: bool = False) -> dict[str, Any]:
        measurement = task.get("reading_measurement")
        if measurement is not None and not remeasure:
            return {"codepoints": int(measurement["serialized_codepoints"]),
                    "fingerprint": str(measurement["input_fingerprint"]),
                    "blocking_code": measurement.get("blocking_code")}
        package = None if remeasure else self._existing_reading_package(
            task, str(batch["actor"]), int(batch["document_registry_revision"]))
        if package is not None:
            projection = self._reading_projection(package["window"], package["materials"])
            return {"codepoints": len(canonical_json(projection).decode("utf-8")),
                    "fingerprint": "sha256:" + fingerprint({"package_id": package["package_id"]}),
                    "blocking_code": None}
        reader_config = self._reader_config()
        observed = self.measure_reading_window(
            task["target_refs"], int(batch["document_registry_revision"]),
            self._unitset_revisions(task["target_refs"]), reader_config,
            actor=str(batch["actor"]))
        return {"codepoints": int(observed["serialized_codepoints"]),
                "fingerprint": str(observed["input_fingerprint"]),
                "blocking_code": observed.get("blocking_code")}

    def _ensure_slices(self, batch: Mapping[str, Any],
                       requested_config: Mapping[str, Any] | None = None,
                       template_id: str = PASS_TEMPLATE_ID,
                       template_hash: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        configured = self._slice_config(requested_config)
        if batch.get("slices_initialized"):
            if batch.get("slice_config") != configured:
                _fail("STALE_PLAN", "slice configuration differs from the initialized batch")
            slices = self._slices(str(batch["batch_id"]))
            by_id = {item["slice_id"]: item for item in slices}
            if set(by_id) != set(batch.get("slice_ids", [])):
                _fail("SOURCE_CHANGED", "slice ledger set differs from the batch control record")
            return dict(batch), [by_id[slice_id] for slice_id in batch.get("slice_ids", [])]

        maximum_tasks = configured["slice_max_tasks"]
        maximum_input = configured["slice_max_input_codepoints"]
        candidates: list[dict[str, Any]] = []
        for task_id in batch["task_ids"]:
            task = self._task(task_id)
            if task["status"] in ("blocked", "failed", "skipped", "completed"):
                continue
            pass_root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/passes")
            if pass_root.exists() and any(pass_root.glob("*.json")):
                continue
            item = self._slice_task_input(batch, task)
            candidates.append({"task_id": task_id, **item})

        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_input = 0
        for item in candidates:
            if current and (len(current) >= maximum_tasks
                            or current_input + item["codepoints"] > maximum_input):
                groups.append(current)
                current, current_input = [], 0
            current.append(item)
            current_input += item["codepoints"]
            if item["codepoints"] > maximum_input:
                groups.append(current)
                current, current_input = [], 0
        if current:
            groups.append(current)

        actual_template_hash = template_hash or _pass_template_hash()
        slices = []
        for ordinal, group in enumerate(groups, 1):
            task_ids = [item["task_id"] for item in group]
            input_codepoints = sum(int(item["codepoints"]) for item in group)
            input_fingerprint = "sha256:" + fingerprint({
                "batch_id": batch["batch_id"], "task_inputs": [
                    {"task_id": item["task_id"], "fingerprint": item["fingerprint"]}
                    for item in group],
                "slice_config": configured, "template_id": template_id,
                "template_hash": actual_template_hash,
            })
            slice_id = f"pass-{ordinal:04d}-{input_fingerprint[-12:]}"
            blocking = next((item["blocking_code"] for item in group if item["blocking_code"]), None)
            if input_codepoints > maximum_input and blocking is None:
                blocking = "SLICE_INPUT_OVERSIZE"
            value = {
                "contract": SLICE_CONTRACT, "slice_id": slice_id,
                "batch_id": batch["batch_id"], "task_ids": task_ids,
                "input_codepoints": input_codepoints,
                "input_fingerprint": input_fingerprint,
                "template_id": template_id, "template_hash": actual_template_hash,
                "state": "blocked" if blocking else "ready", "attempt": 0,
                "lease": {"worker_id": None, "claimed_at": None,
                          "heartbeat_at": None, "expires_at": None},
                "retry_at": None, "result_refs": [],
                "last_error": ({"code": blocking, "message": "slice input cannot fit configured bound",
                                "retryable": False, "at": None,
                                "classification": _failure_class(str(blocking)),
                                "action": "blocked", "output_ref": None} if blocking else None),
                "reslice_count": 0,
                "revision": 1,
            }
            path = self._slice_path(str(batch["batch_id"]), slice_id)
            if path.exists():
                existing = self._slice(str(batch["batch_id"]), slice_id)
                if existing != value:
                    _fail("IDEMPOTENCY_CONFLICT", "deterministic slice id has different content")
            else:
                self._write_slice(value)
            slices.append(value)

        updated = copy.deepcopy(dict(batch))
        updated.update({"revision": int(updated["revision"]) + 1,
                        "slices_initialized": True,
                        "slice_ids": [item["slice_id"] for item in slices],
                        "slice_config": configured,
                        "cancel_requested": bool(updated.get("cancel_requested", False)),
                        "last_operation": "slice"})
        validate_record("knowledge_batch", updated)
        _write_atomic(self._batch_path(str(updated["batch_id"])), _json_bytes(updated))
        return updated, slices

    @staticmethod
    def _clear_lease(value: dict[str, Any]) -> None:
        value["lease"] = {"worker_id": None, "claimed_at": None,
                          "heartbeat_at": None, "expires_at": None}

    def batch_next_slice(self, batch_id: str, worker_id: str,
                         config: Mapping[str, Any] | None = None,
                         lease_seconds: int | None = None,
                         now: datetime | str | None = None,
                         slice_id: str | None = None) -> dict[str, Any]:
        if not worker_id.strip():
            _fail("INVALID_SCHEMA", "worker_id is required", "$.worker_id")
        moment, stamp = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch.get("cancel_requested"):
                return {"ok": True, "leased": False, "reason": "batch_cancelled", "slice": None}
            cooldown_until = batch.get("cooldown_until")
            if cooldown_until is not None and _parse_utc(str(cooldown_until)) > moment:
                return {"ok": True, "leased": False, "reason": "batch_cooldown",
                        "cooldown_until": cooldown_until, "slice": None}
            if cooldown_until is not None:
                batch = copy.deepcopy(batch)
                batch.update({"cooldown_until": None, "cooldown_reason": None,
                              "revision": int(batch["revision"]) + 1,
                              "last_operation": "slice"})
                validate_record("knowledge_batch", batch)
                _write_atomic(self._batch_path(batch_id), _json_bytes(batch))
            effective_config = (batch.get("slice_config") if batch.get("slices_initialized")
                                and config is None else config)
            batch, slices = self._ensure_slices(batch, effective_config)
            changed = []
            for item in slices:
                if (item["state"] == "retry_wait" and item["retry_at"] is not None
                        and _parse_utc(item["retry_at"]) <= moment):
                    item = copy.deepcopy(item)
                    item.update({"state": "ready", "retry_at": None,
                                 "revision": int(item["revision"]) + 1})
                    self._write_slice(item)
                    changed.append(item)
            if changed:
                by_id = {item["slice_id"]: item for item in slices}
                by_id.update({item["slice_id"]: item for item in changed})
                slices = [by_id[slice_id] for slice_id in batch["slice_ids"]]
            selected = (next((item for item in slices if item["slice_id"] == slice_id), None)
                        if slice_id is not None else None)
            if slice_id is not None and selected is None:
                _fail("INVALID_SCHEMA", "slice is not part of batch", "$.slice_id")
            if selected is not None and selected["state"] == "leased":
                if (selected["lease"]["worker_id"] == worker_id
                        and _parse_utc(selected["lease"]["expires_at"]) > moment):
                    return {"ok": True, "leased": True, "reason": "existing_lease",
                            "slice": selected}
                return {"ok": True, "leased": False, "reason": "slice_leased", "slice": None}
            leased_count = sum(item["state"] == "leased" for item in slices)
            if leased_count >= int(batch["slice_config"]["pass_worker_concurrency"]):
                return {"ok": True, "leased": False, "reason": "concurrency_limit", "slice": None}
            if slice_id is None:
                selected = next((item for item in slices if item["state"] == "ready"), None)
            if selected is None:
                return {"ok": True, "leased": False, "reason": "no_ready_slice", "slice": None}
            if selected["state"] != "ready":
                return {"ok": True, "leased": False, "reason": "slice_not_ready", "slice": None}
            duration = int(batch["slice_config"]["lease_seconds"]
                           if lease_seconds is None else lease_seconds)
            if duration < 1:
                _fail("INVALID_SCHEMA", "lease_seconds must be positive", "$.lease_seconds")
            selected = copy.deepcopy(selected)
            selected.update({"state": "leased", "attempt": int(selected["attempt"]) + 1,
                             "retry_at": None, "revision": int(selected["revision"]) + 1})
            selected["lease"] = {"worker_id": worker_id, "claimed_at": stamp,
                                 "heartbeat_at": stamp,
                                 "expires_at": (moment + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")}
            self._write_slice(selected)
            return {"ok": True, "leased": True, "reason": "", "slice": selected}

    def initialize_slices(self, batch_id: str, actor: str,
                          expected_revision: int,
                          config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Create deterministic slice records without issuing any worker lease."""
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "batch actor differs", "$.actor")
            if batch["revision"] != expected_revision:
                _fail("REVISION_CONFLICT", "batch revision changed", "$.expected_revision")
            if batch.get("cancel_requested"):
                _fail("CANCELLED", "batch is cancelled")
            effective_config = (batch.get("slice_config") if config is None
                                and batch.get("slices_initialized") else config)
            updated, slices = self._ensure_slices(batch, effective_config)
            return {"ok": True, "batch": updated,
                    "slice_ids": [item["slice_id"] for item in slices]}

    def refresh_ready_slices(self, batch_id: str, actor: str,
                             now: datetime | str | None = None) -> dict[str, Any]:
        """Make elapsed retry/cooldown windows visible to an external scheduler."""
        moment, _ = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "batch actor differs")
            if batch.get("cancel_requested"):
                return {"ok": True, "batch": batch, "ready_slice_ids": []}
            cooldown = batch.get("cooldown_until")
            if cooldown is not None and _parse_utc(cooldown) > moment:
                return {"ok": True, "batch": batch, "ready_slice_ids": []}
            ready = []
            for value in self._slices(batch_id):
                if (value["state"] == "retry_wait" and value["retry_at"] is not None
                        and _parse_utc(value["retry_at"]) <= moment):
                    updated = copy.deepcopy(value)
                    updated.update({"state": "ready", "retry_at": None,
                                    "revision": int(updated["revision"]) + 1})
                    self._write_slice(updated)
                    ready.append(updated["slice_id"])
            if cooldown is not None:
                batch = copy.deepcopy(batch)
                batch.update({"cooldown_until": None, "cooldown_reason": None,
                              "revision": int(batch["revision"]) + 1,
                              "last_operation": "slice"})
                validate_record("knowledge_batch", batch)
                _write_atomic(self._batch_path(batch_id), _json_bytes(batch))
            return {"ok": True, "batch": batch, "ready_slice_ids": ready}

    def slice_heartbeat(self, batch_id: str, slice_id: str, worker_id: str,
                        expected_revision: int,
                        now: datetime | str | None = None) -> dict[str, Any]:
        moment, stamp = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            value = self._slice(batch_id, slice_id)
            if batch.get("cancel_requested") or value["state"] == "cancelled":
                _fail("CANCELLED", "slice heartbeat rejected after batch cancellation")
            if value["revision"] != expected_revision:
                _fail("REVISION_CONFLICT", "slice revision changed", "$.expected_revision")
            if value["state"] != "leased" or value["lease"]["worker_id"] != worker_id:
                _fail("ACCESS_DENIED", "slice is not leased by this worker")
            if _parse_utc(value["lease"]["expires_at"]) <= moment:
                _fail("LEASE_EXPIRED", "slice lease expired before heartbeat")
            updated = copy.deepcopy(value)
            updated["lease"]["heartbeat_at"] = stamp
            updated["lease"]["expires_at"] = (
                moment + timedelta(seconds=int(batch["slice_config"]["lease_seconds"])
                                   )).isoformat().replace("+00:00", "Z")
            updated["revision"] += 1
            self._write_slice(updated)
            return {"ok": True, "slice": updated}

    def slice_complete(self, request: Mapping[str, Any],
                       now: datetime | str | None = None) -> dict[str, Any]:
        batch_id, slice_id = str(request["batch_id"]), str(request["slice_id"])
        worker_id, expected = str(request["worker_id"]), int(request["expected_revision"])
        result_refs = [str(item) for item in request.get("result_refs", [])]
        if not result_refs:
            _fail("INVALID_SCHEMA", "completed slice requires at least one result reference",
                  "$.result_refs")
        moment, _ = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            value = self._slice(batch_id, slice_id)
            if batch.get("cancel_requested") or value["state"] == "cancelled":
                _fail("CANCELLED", "slice completion rejected after batch cancellation")
            if value["revision"] != expected:
                _fail("REVISION_CONFLICT", "slice revision changed", "$.expected_revision")
            if value["state"] != "leased" or value["lease"]["worker_id"] != worker_id:
                _fail("ACCESS_DENIED", "slice is not leased by this worker")
            if _parse_utc(value["lease"]["expires_at"]) <= moment:
                _fail("LEASE_EXPIRED", "slice lease expired before completion")
            updated = copy.deepcopy(value)
            updated.update({"state": "completed", "result_refs": result_refs,
                            "retry_at": None, "last_error": None,
                            "revision": int(updated["revision"]) + 1})
            self._clear_lease(updated)
            self._write_slice(updated)
            return {"ok": True, "slice": updated}

    def slice_fail(self, request: Mapping[str, Any],
                   now: datetime | str | None = None) -> dict[str, Any]:
        batch_id, slice_id = str(request["batch_id"]), str(request["slice_id"])
        worker_id, expected = str(request["worker_id"]), int(request["expected_revision"])
        moment, stamp = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            value = self._slice(batch_id, slice_id)
            if batch.get("cancel_requested") or value["state"] == "cancelled":
                _fail("CANCELLED", "slice failure rejected after batch cancellation")
            if value["revision"] != expected:
                _fail("REVISION_CONFLICT", "slice revision changed", "$.expected_revision")
            if value["state"] != "leased" or value["lease"]["worker_id"] != worker_id:
                _fail("ACCESS_DENIED", "slice is not leased by this worker")
            if _parse_utc(value["lease"]["expires_at"]) <= moment:
                _fail("LEASE_EXPIRED", "slice lease expired before failure recording")
            code = str(request["code"])
            classification = _failure_class(code)
            attempt = int(value["attempt"])
            configured_attempts = int(batch["slice_config"]["max_attempts"])
            retry_limit = min(configured_attempts, 3) if classification == "invalid_model_output" else configured_attempts
            retry = classification in ("rate_limited", "transient", "invalid_model_output") \
                and attempt < retry_limit
            retry_after = TRANSIENT_BACKOFF_SECONDS[min(max(attempt - 1, 0),
                                                          len(TRANSIENT_BACKOFF_SECONDS) - 1)]
            output_ref = None
            if classification == "invalid_model_output":
                if "failed_output" not in request:
                    _fail("INVALID_SCHEMA", "invalid model JSON requires failed_output for audit",
                          "$.failed_output")
                output = str(request["failed_output"])
                output_path = self._slice_root(batch_id).parent / "failures" / (
                    f"{slice_id}-attempt-{attempt:04d}.txt")
                encoded = output.encode("utf-8")
                if output_path.exists() and output_path.read_bytes() != encoded:
                    _fail("IDEMPOTENCY_CONFLICT", "failed model output differs for the same attempt")
                if not output_path.exists():
                    _write_atomic(output_path, encoded)
                output_ref = output_path.relative_to(self.vault).as_posix()

            reslice_count = int(value.get("reslice_count", 0))
            if retry:
                state, action = "retry_wait", "retry"
            elif classification == "stale_input":
                state, action = "reconcile_required", "reconcile"
            elif classification == "reading_budget" and reslice_count < 1:
                state, action = "reconcile_required", "remeasure_and_reslice"
                reslice_count += 1
            elif classification == "awaiting_approval":
                state, action = "awaiting_approval", "await_approval"
            else:
                state, action = "blocked", "blocked"
            updated = copy.deepcopy(value)
            updated.update({"state": state,
                            "retry_at": ((moment + timedelta(seconds=retry_after))
                                         .isoformat().replace("+00:00", "Z") if retry else None),
                            "last_error": {"code": code,
                                           "message": str(request["message"]),
                                           "retryable": retry, "at": stamp,
                                           "classification": classification,
                                           "action": action, "output_ref": output_ref},
                            "reslice_count": reslice_count,
                            "revision": int(updated["revision"]) + 1})
            self._clear_lease(updated)
            self._write_slice(updated)
            updated_batch = batch
            if classification == "rate_limited":
                updated_batch = copy.deepcopy(batch)
                updated_batch.update({
                    "cooldown_until": (moment + timedelta(seconds=retry_after)
                                       ).isoformat().replace("+00:00", "Z"),
                    "cooldown_reason": code,
                    "last_operation": "slice",
                    "revision": int(updated_batch["revision"]) + 1,
                })
                validate_record("knowledge_batch", updated_batch)
                _write_atomic(self._batch_path(batch_id), _json_bytes(updated_batch))
            return {"ok": True, "slice": updated, "batch": updated_batch}

    def reconcile_slice_inputs(self, batch_id: str, slice_id: str, actor: str,
                               expected_revision: int) -> dict[str, Any]:
        """Remeasure and replace one budget-stale slice; replacements cannot reslice again."""
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch.get("cancel_requested"):
                _fail("CANCELLED", "slice reconciliation rejected after batch cancellation")
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "only the batch actor can reconcile slice inputs")
            value = self._slice(batch_id, slice_id)
            if value["revision"] != expected_revision:
                _fail("REVISION_CONFLICT", "slice revision changed", "$.expected_revision")
            if (value["state"] != "reconcile_required"
                    or (value.get("last_error") or {}).get("classification") != "reading_budget"
                    or int(value.get("reslice_count", 0)) != 1):
                _fail("INVALID_STATE", "slice is not eligible for its one budget reslice")

            config = batch.get("slice_config")
            if config is None:
                _fail("INVALID_STATE", "slice configuration is unavailable")
            items = []
            for task_id in value["task_ids"]:
                task = self._task(task_id)
                measured = self._slice_task_input(batch, task, remeasure=True)
                items.append({"task_id": task_id, **measured})
            groups, current, current_input = [], [], 0
            for item in items:
                if current and (len(current) >= int(config["slice_max_tasks"])
                                or current_input + int(item["codepoints"])
                                > int(config["slice_max_input_codepoints"])):
                    groups.append(current)
                    current, current_input = [], 0
                current.append(item)
                current_input += int(item["codepoints"])
                if int(item["codepoints"]) > int(config["slice_max_input_codepoints"]):
                    groups.append(current)
                    current, current_input = [], 0
            if current:
                groups.append(current)

            replacements = []
            for ordinal, group in enumerate(groups, 1):
                input_codepoints = sum(int(item["codepoints"]) for item in group)
                input_fingerprint = "sha256:" + fingerprint({
                    "batch_id": batch_id, "supersedes": slice_id,
                    "reslice_count": 1,
                    "task_inputs": [{"task_id": item["task_id"],
                                     "fingerprint": item["fingerprint"]}
                                    for item in group],
                    "slice_config": config, "template_id": value["template_id"],
                    "template_hash": value["template_hash"],
                })
                replacement_id = f"pass-r1-{ordinal:04d}-{input_fingerprint[-12:]}"
                blocking = next((item["blocking_code"] for item in group
                                 if item["blocking_code"]), None)
                if input_codepoints > int(config["slice_max_input_codepoints"]) and blocking is None:
                    blocking = "SLICE_INPUT_OVERSIZE"
                replacement = {
                    "contract": SLICE_CONTRACT, "slice_id": replacement_id,
                    "batch_id": batch_id,
                    "task_ids": [item["task_id"] for item in group],
                    "input_codepoints": input_codepoints,
                    "input_fingerprint": input_fingerprint,
                    "template_id": value["template_id"],
                    "template_hash": value["template_hash"],
                    "state": "blocked" if blocking else "ready", "attempt": 0,
                    "lease": {"worker_id": None, "claimed_at": None,
                              "heartbeat_at": None, "expires_at": None},
                    "retry_at": None, "result_refs": [], "reslice_count": 1,
                    "last_error": ({"code": str(blocking),
                                    "message": "remeasured slice input cannot fit configured bound",
                                    "retryable": False, "at": None,
                                    "classification": _failure_class(str(blocking)),
                                    "action": "blocked", "output_ref": None}
                                   if blocking else None),
                    "revision": 1,
                }
                path = self._slice_path(batch_id, replacement_id)
                if path.exists() and self._slice(batch_id, replacement_id) != replacement:
                    _fail("IDEMPOTENCY_CONFLICT", "replacement slice id has different content")
                if not path.exists():
                    self._write_slice(replacement)
                replacements.append(replacement)

            superseded = copy.deepcopy(value)
            superseded.update({"state": "cancelled", "retry_at": None,
                               "revision": int(superseded["revision"]) + 1})
            self._clear_lease(superseded)
            self._write_slice(superseded)
            slice_ids = list(batch["slice_ids"])
            position = slice_ids.index(slice_id)
            slice_ids[position + 1:position + 1] = [item["slice_id"] for item in replacements]
            updated_batch = copy.deepcopy(batch)
            updated_batch.update({"slice_ids": slice_ids, "last_operation": "slice",
                                  "revision": int(updated_batch["revision"]) + 1})
            validate_record("knowledge_batch", updated_batch)
            _write_atomic(self._batch_path(batch_id), _json_bytes(updated_batch))
            return {"ok": True, "batch": updated_batch, "superseded": superseded,
                    "replacements": replacements}

    def reclaim_expired_slices(self, batch_id: str,
                               now: datetime | str | None = None) -> dict[str, Any]:
        moment, stamp = _utc(now)
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            reclaimed = []
            for value in self._slices(batch_id):
                if (value["state"] != "leased" or value["lease"]["expires_at"] is None
                        or _parse_utc(value["lease"]["expires_at"]) > moment):
                    continue
                updated = copy.deepcopy(value)
                retry = int(updated["attempt"]) < int(batch["slice_config"]["max_attempts"])
                updated.update({"state": "ready" if retry else "blocked", "retry_at": None,
                                "last_error": {"code": "LEASE_EXPIRED",
                                               "message": "worker lease expired",
                                               "retryable": retry, "at": stamp},
                                "revision": int(updated["revision"]) + 1})
                self._clear_lease(updated)
                self._write_slice(updated)
                reclaimed.append(updated)
            return {"ok": True, "reclaimed": len(reclaimed), "slices": reclaimed}

    def cancel_batch(self, batch_id: str, actor: str, expected_revision: int) -> dict[str, Any]:
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "only the batch actor can cancel it")
            if batch["revision"] != expected_revision:
                _fail("REVISION_CONFLICT", "batch revision changed", "$.expected_revision")
            cancelled = []
            for value in self._slices(batch_id):
                if value["state"] in ("completed", "cancelled"):
                    continue
                updated = copy.deepcopy(value)
                updated.update({"state": "cancelled", "retry_at": None,
                                "revision": int(updated["revision"]) + 1})
                self._clear_lease(updated)
                self._write_slice(updated)
                cancelled.append(updated["slice_id"])
            updated_batch = copy.deepcopy(batch)
            updated_batch.update({"cancel_requested": True, "state": "cancelled",
                                  "last_operation": "cancel",
                                  "revision": int(updated_batch["revision"]) + 1})
            validate_record("knowledge_batch", updated_batch)
            _write_atomic(self._batch_path(batch_id), _json_bytes(updated_batch))
            return {"ok": True, "batch": updated_batch, "cancelled_slice_ids": cancelled}

    def resume_cancelled_batch(self, batch_id: str, actor: str,
                               expected_revision: int) -> dict[str, Any]:
        """Reopen cancelled slices; worker admission still checks pinned inputs."""
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "only the batch actor can resume it")
            if batch["revision"] != expected_revision:
                _fail("REVISION_CONFLICT", "batch revision changed", "$.expected_revision")
            if not batch.get("cancel_requested") or batch["state"] != "cancelled":
                _fail("INVALID_STATE", "batch is not cancelled")
            reopened = []
            for value in self._slices(batch_id):
                if value["state"] != "cancelled":
                    continue
                updated = copy.deepcopy(value)
                updated.update({"state": "ready", "retry_at": None,
                                "revision": int(updated["revision"]) + 1})
                self._clear_lease(updated)
                self._write_slice(updated)
                reopened.append(updated["slice_id"])
            updated_batch = copy.deepcopy(batch)
            updated_batch.update({"cancel_requested": False, "state": "analyzing",
                                  "last_operation": "slice",
                                  "revision": int(updated_batch["revision"]) + 1})
            validate_record("knowledge_batch", updated_batch)
            _write_atomic(self._batch_path(batch_id), _json_bytes(updated_batch))
            return {"ok": True, "batch": updated_batch, "reopened_slice_ids": reopened}

    def batch_status(self, batch_id: str, compact: bool = False) -> dict[str, Any]:
        batch = self._batch(batch_id)
        tasks = [self._task(task_id) for task_id in batch["task_ids"]]
        slices = self._slices(batch_id)
        resource_reductions = [self._resource_reduction(batch_id, reduction_id)
                               for reduction_id in batch.get("resource_reduction_ids", [])]
        global_reduction = (self._global_reduction(batch_id, batch["global_reduction_id"])
                            if batch.get("global_reduction_id") is not None else None)
        slice_counts: dict[str, int] = {}
        for item in slices:
            slice_counts[item["state"]] = slice_counts.get(item["state"], 0) + 1
        task_counts: dict[str, int] = {}
        task_details = []
        for task in tasks:
            task_counts[task["status"]] = task_counts.get(task["status"], 0) + 1
            task_root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task['task_id']}")
            readings = sorted((task_root / "readings").glob("*.json")) if (task_root / "readings").exists() else []
            passes = sorted((task_root / "passes").glob("*.json")) if (task_root / "passes").exists() else []
            task_details.append({"task_id": task["task_id"], "revision": task["revision"],
                                 "status": task["status"], "reading_packages": len(readings),
                                 "passes": len(passes)})
        run_details = []
        covered: dict[str, list[str]] = {}
        for run_id in batch["run_ids"]:
            path = self._run_path(run_id)
            if not path.is_file():
                run_details.append({"run_id": run_id, "state": "missing", "revision": None})
                continue
            run = _load_json(path)
            validate_record("build_run", run)
            run_details.append({"run_id": run_id, "state": run["state"], "revision": run["revision"]})
            for snapshot in run["tasks"]:
                covered.setdefault(snapshot["task_id"], []).append(run_id)
        duplicate_task_runs = sorted(task_id for task_id, values in covered.items() if len(values) > 1)
        uncovered = sorted(task["task_id"] for task in tasks
                           if task["status"] not in ("blocked", "failed", "skipped")
                           and task["task_id"] not in covered)
        next_actions = []
        if not batch.get("cancel_requested") and batch.get("cooldown_until") is not None:
            next_actions.append("wait-batch-cooldown")
        elif not batch.get("cancel_requested") and (not batch.get("slices_initialized") or any(
                item["state"] in ("ready", "retry_wait") for item in slices)):
            next_actions.append("batch-next-slice")
        if not batch.get("cancel_requested") and any(item["state"] == "leased" for item in slices):
            next_actions.append("slice-heartbeat-or-complete")
        if not batch.get("cancel_requested") and any(item["state"] == "blocked" for item in slices):
            next_actions.append("inspect-blocked-slices")
        if not batch.get("cancel_requested") and any(
                item["state"] == "reconcile_required" for item in slices):
            next_actions.append("reconcile-slice-inputs")
        if not batch.get("cancel_requested") and any(
                item["state"] == "awaiting_approval" for item in slices):
            next_actions.append("await-human-approval")
        if not batch.get("cancel_requested"):
            if any(item["status"] == "pending" or (item["status"] == "running" and not item["reading_packages"])
                   for item in task_details):
                next_actions.append("batch-prepare")
            if any(item["status"] == "running" and item["reading_packages"] and not item["passes"]
                   for item in task_details):
                next_actions.append("batch-pass")
            reducible = {item["task_id"] for item in task_details
                         if item["status"] == "running" and item["passes"]
                         and item["task_id"] in uncovered}
            resource_covered = {snapshot["task_id"] for reduction in resource_reductions
                                for snapshot in reduction["tasks"]}
            if reducible - resource_covered:
                next_actions.append("batch-resource-reduce")
            elif (reducible and global_reduction is None
                  and not any(item["status"] == "pending" or
                              (item["status"] == "running" and not item["passes"])
                              for item in task_details)):
                next_actions.append("batch-global-reduce")
            if batch["run_ids"]:
                next_actions.append("batch-validate")
            if any(item["state"] == "draft" for item in run_details):
                next_actions.append("human-checkpoint-1-then-batch-finalize")
            if run_details and all(item["state"] == "completed" for item in run_details):
                next_actions.append("vault-finalize-plan")
        status = {"ok": True, "batch": batch, "task_counts": task_counts,
                  "slice_counts": slice_counts,
                  "runs": run_details,
                  "duplicate_task_run_ids": duplicate_task_runs,
                  "next_actions": list(dict.fromkeys(next_actions))}
        if compact:
            status["coverage"] = {"total_tasks": len(tasks),
                                  "reading_packages": sum(item["reading_packages"] for item in task_details),
                                  "passes": sum(item["passes"] for item in task_details),
                                  "uncovered_tasks": len(uncovered),
                                  "total_slices": len(slices),
                                  "completed_slices": slice_counts.get("completed", 0),
                                  "resource_reductions": len(resource_reductions),
                                  "global_reduction_complete": global_reduction is not None}
            status["failure_count"] = len(batch["failures"])
        else:
            status["tasks"] = task_details
            status["slices"] = slices
            status["resource_reductions"] = [{
                "reduction_id": item["reduction_id"], "resource_id": item["resource_id"],
                "task_count": len(item["tasks"]), "proposal_count": len(item["proposals"]),
                "output_fingerprint": item["output_fingerprint"],
            } for item in resource_reductions]
            status["global_reduction"] = global_reduction
            status["uncovered_task_ids"] = uncovered
        return status

    def resume_batch(self, batch_id: str) -> dict[str, Any]:
        """Return the authoritative recovery point; stage commands are idempotent."""
        return self.batch_status(batch_id)

    def _existing_reading_package(self, task: Mapping[str, Any], actor: str,
                                  registry_revision: int) -> dict[str, Any] | None:
        root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task['task_id']}/readings")
        for path in reversed(sorted(root.glob("*.json"))) if root.exists() else []:
            package = _load_json(path)
            validate_record("reading_package", package)
            if (package["task_id"] == task["task_id"] and package["actor"] == actor
                    and package["document_registry_revision"] == registry_revision
                    and package["window"]["core_refs"] == task["target_refs"]):
                return package
        return None

    def _verify_planned_measurement(self, task: Mapping[str, Any], actor: str,
                                    registry_revision: int,
                                    max_codepoints: int | None) -> dict[str, Any] | None:
        planned = task.get("reading_measurement")
        if planned is None:
            return None
        reader_config = self._reader_config(max_codepoints)
        if fingerprint(reader_config) != task.get("reader_config_hash"):
            _fail("STALE_PLAN", "reader configuration changed after exact batch planning")
        observed = self.measure_reading_window(
            task["target_refs"], registry_revision, planned["unitset_revisions"],
            reader_config, actor=actor)
        if observed != planned:
            _fail("STALE_PLAN", "reading-window measurement changed after batch planning")
        if not observed["fits"]:
            _fail(str(observed["blocking_code"]), "planned reading window no longer fits")
        return observed

    def prepare_batch(self, batch_id: str, actor: str, registry_revision: int,
                      max_codepoints: int | None = None) -> dict[str, Any]:
        """Claim ready tasks and persist bounded reading packages in one invocation."""
        self.source.enable_session_cache()
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "batch prepare actor does not own the batch")
            if batch["document_registry_revision"] != registry_revision:
                _fail("REVISION_CONFLICT", "batch prepare must use the pinned document registry revision")
            results, failures = [], []
            for task_id in batch["task_ids"]:
                task = self._task(task_id)
                if task["status"] in ("blocked", "failed", "skipped", "completed"):
                    results.append({"task_id": task_id, "status": task["status"], "prepared": False})
                    continue
                try:
                    measurement = self._verify_planned_measurement(
                        task, actor, registry_revision, max_codepoints)
                    if task["status"] == "pending":
                        task = self.claim_task(task_id, actor, task["revision"])["task"]
                    elif task["status"] != "running" or task["actor"] != actor:
                        _fail("ACCESS_DENIED", "batch task is not claimable by the batch actor")
                    package = self._existing_reading_package(task, actor, registry_revision)
                    created = package is None
                    if package is None:
                        package = self.reading_material(
                            task_id, actor, registry_revision, max_codepoints)["package"]
                    results.append({"task_id": task_id, "status": "running", "prepared": True,
                                    "created": created, "reading_package_id": package["package_id"],
                                    "total_codepoints": package["total_codepoints"],
                                    "measurement": measurement})
                except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                    failures.append(self._failure("prepare", task_id, exc))
                    results.append({"task_id": task_id, "status": "error", "prepared": False})
            updated = self._store_batch(batch, state="analyzing", operation="prepare", failures=failures)
            return {"ok": not failures, "batch": updated, "results": results, "failures": failures}

    def claim_task(self, task_id: str, actor: str, expected_revision: int) -> dict[str, Any]:
        task = self._task(task_id)
        if (task["status"] == "running" and task["actor"] == actor
                and task["revision"] in (expected_revision, expected_revision + 1)):
            return {"ok": True, "claimed": False, "task": task}
        if task["revision"] != expected_revision:
            _fail("REVISION_CONFLICT", "task revision changed", "$.expected_revision")
        if task["status"] in ("completed", "skipped"):
            _fail("INVALID_STATE", "terminal task cannot be claimed")
        if task["status"] == "running":
            _fail("REVISION_CONFLICT", "task is already claimed by another actor")
        previous = task["status"]
        task["status"], task["actor"] = "running", actor
        task["revision"] += 1
        if previous in ("blocked", "failed"):
            task["attempt"] += 1
        task["reason"] = ""
        validate_record("work", task)
        _write_atomic(self._task_path(task_id), _json_bytes(task))
        return {"ok": True, "claimed": True, "task": task}

    def set_task_state(self, task_id: str, actor: str, expected_revision: int,
                       status: str, reason: str,
                       deferred: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Record a recoverable stop without turning it into knowledge completion."""
        task = self._task(task_id)
        if task["revision"] != expected_revision:
            _fail("REVISION_CONFLICT", "task revision changed", "$.expected_revision")
        if task["actor"] != actor:
            _fail("ACCESS_DENIED", "only the current task actor can change task state")
        if status not in ("blocked", "failed", "skipped"):
            _fail("INVALID_STATE", "state update must be blocked, failed or skipped", "$.status")
        task.update({"status": status, "reason": str(reason),
                     "deferred": [dict(item) for item in deferred],
                     "revision": task["revision"] + 1})
        units = self._live_units(
            [*task["target_refs"], *[item["source_ref"] for item in task["deferred"]]],
            actor, self._document_registry_revision())
        validate_references("work", task, units)
        _write_atomic(self._task_path(task_id), _json_bytes(task))
        return {"ok": True, "task": task}

    def set_task_state_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Apply explicit terminal/blocked transitions without ad-hoc task loops."""
        operation_id = str(request["operation_id"])
        actor = str(request["actor"])
        transitions = list(request["transitions"])
        task_ids = [str(item["task_id"]) for item in transitions]
        if not transitions or len(set(task_ids)) != len(task_ids):
            _fail("INVALID_SCHEMA", "batch state update requires unique tasks", "$.transitions")
        with _exclusive_lock(self._batch_lock_path(operation_id)):
            for item in transitions:
                task = self._task(str(item["task_id"]))
                if task["actor"] != actor or task["revision"] != int(item["expected_revision"]):
                    _fail("REVISION_CONFLICT", "batch state preflight found actor/revision drift",
                          "$.transitions")
            results = []
            for item in transitions:
                result = self.set_task_state(
                    str(item["task_id"]), actor, int(item["expected_revision"]),
                    str(item["status"]), str(item["reason"]), item.get("deferred", []))
                results.append({"task_id": result["task"]["task_id"],
                                "status": result["task"]["status"],
                                "revision": result["task"]["revision"]})
            return {"ok": True, "operation_id": operation_id, "results": results}

    def reading_material(self, task_id: str, actor: str, registry_revision: int,
                         max_codepoints: int | None = None) -> dict[str, Any]:
        task = self._task(task_id)
        if task["status"] != "running" or task["actor"] != actor:
            _fail("ACCESS_DENIED", "task must be claimed by the reading actor")
        reader_config = self._reader_config(max_codepoints)
        assembled = self._assemble_reading(
            list(task["target_refs"]), actor, registry_revision, reader_config)
        window, materials = assembled["window"], assembled["materials"]
        measurement = assembled["measurement"]
        if not measurement["fits"]:
            _fail(str(measurement["blocking_code"]),
                  f"serialized reading window uses {measurement['serialized_codepoints']} "
                  f"of {measurement['limit']} codepoints")
        planned = task.get("reading_measurement")
        if planned is not None and measurement != planned:
            _fail("STALE_PLAN", "reading-window measurement differs from the exact plan")
        package = {
            "contract": "hermes-reading-package/v1", "task_id": task_id,
            "task_revision": task["revision"], "actor": actor,
            "document_registry_revision": registry_revision, "window": window,
            "materials": materials,
            "total_codepoints": sum(len(item["core_text"] or "") for item in materials),
        }
        package["package_id"] = fingerprint(package)
        validate_record("reading_package", package)
        target = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/readings/{package['package_id']}.json")
        if target.exists() and _load_json(target) != package:
            _fail("SOURCE_CHANGED", "reading-package identity collision")
        if not target.exists():
            _write_atomic(target, _json_bytes(package))
        return {"ok": True, "task_id": task_id, "task_revision": task["revision"],
                "package": package, "window": window, "measurement": measurement,
                "core": assembled["core"], "context": assembled["context"]}

    @staticmethod
    def _reading_package_fingerprint(package: Mapping[str, Any], package_id: str) -> str:
        unsigned = copy.deepcopy(dict(package))
        stored_id = str(unsigned.pop("package_id", ""))
        if stored_id != package_id or fingerprint(unsigned) != stored_id:
            _fail("STALE_INPUT", "reading package fingerprint no longer matches its content")
        return "sha256:" + stored_id

    @staticmethod
    def _same_pass_payload(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        fields = ("contract", "task_id", "task_revision", "pass_kind", "sequence",
                  "reading_window_id", "reading_package_id", "inspections", "candidates",
                  "empty_reason", "actor")
        return all(left.get(field) == right.get(field) for field in fields)

    def record_pass(self, request: Mapping[str, Any], *, batch_id: str | None = None,
                    template_hash: str | None = None) -> dict[str, Any]:
        task_id, actor = str(request["task_id"]), str(request["actor"])
        task = self._task(task_id)
        expected_revision = int(request["expected_revision"])
        package_id = str(request["reading_package_id"])
        package_path = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/readings/{package_id}.json")
        package = _load_json(package_path)
        validate_record("reading_package", package)
        package_fingerprint = self._reading_package_fingerprint(package, package_id)
        window = package["window"]
        if window["core_refs"] != task["target_refs"]:
            _fail("STALE_INPUT", "reading window does not belong to the current task")
        body = {"contract": "hermes-knowledge-pass/v1", "task_id": task_id,
                "task_revision": expected_revision, "pass_kind": str(request["pass_kind"]),
                "sequence": int(request["sequence"]), "reading_window_id": window["window_id"],
                "reading_package_id": package_id,
                "inspections": list(request["inspections"]), "candidates": list(request["candidates"]),
                "empty_reason": str(request.get("empty_reason", "")), "actor": actor}
        if batch_id is not None:
            actual_template_hash = str(template_hash or _pass_template_hash())
            if not re.fullmatch(r"[0-9a-f]{64}", actual_template_hash):
                _fail("INVALID_SCHEMA", "template_hash must be a sha256 hex digest",
                      "$.template_hash")
            body.update({
                "batch_id": batch_id,
                "reading_package_fingerprint": package_fingerprint,
                "template_hash": actual_template_hash,
                "idempotency_key": "sha256:" + fingerprint({
                    "batch_id": batch_id, "task_id": task_id,
                    "pass_sequence": body["sequence"],
                    "reading_package_fingerprint": package_fingerprint,
                    "template_hash": actual_template_hash,
                }),
            })
        body["pass_id"] = fingerprint(body)
        validate_record("knowledge_pass", body)
        pass_root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/passes")
        path = pass_root / f"{body['sequence']:04d}-{body['pass_id']}.json"
        existing_sequence = list(pass_root.glob(f"{body['sequence']:04d}-*.json")) if pass_root.exists() else []
        if existing_sequence:
            if len(existing_sequence) != 1:
                _fail("DUPLICATE", "pass sequence contains multiple records", "$.sequence")
            existing_path = existing_sequence[0]
            existing = _load_json(existing_path)
            validate_record("knowledge_pass", existing)
            if existing == body or (existing.get("idempotency_key") is None
                                    and self._same_pass_payload(existing, body)):
                return {"ok": True, "created": False, "pass": existing, "task": task,
                        "idempotency_key": body.get("idempotency_key"),
                        "path": existing_path.relative_to(self.vault).as_posix()}
            if (body.get("idempotency_key") is not None
                    and existing.get("idempotency_key") == body["idempotency_key"]):
                _fail("IDEMPOTENCY_CONFLICT",
                      "same Pass idempotency key has different content", "$.idempotency_key")
            _fail("DUPLICATE", "pass sequence already contains different content", "$.sequence")
        if task["revision"] != expected_revision:
            _fail("STALE_INPUT", "task revision changed", "$.expected_revision")
        if task["status"] != "running" or task["actor"] != actor:
            _fail("ACCESS_DENIED", "pass actor must own the running task")
        if package["task_id"] != task_id or package["actor"] != actor:
            _fail("STALE_INPUT", "reading package belongs to another task or actor")
        if int(request["registry_revision"]) != package["document_registry_revision"]:
            _fail("STALE_INPUT",
                  "pass must use the registry revision observed by its reading package")
        existing_passes = sorted(pass_root.glob("*.json")) if pass_root.exists() else []
        sequences = [int(item.name.split("-", 1)[0]) for item in existing_passes]
        expected_sequence = 0 if not sequences else max(sequences) + 1
        if body["sequence"] != expected_sequence:
            _fail("INVALID_PASS", f"next pass sequence must be {expected_sequence}", "$.sequence")
        if body["sequence"] == 0 and body["pass_kind"] != "candidate":
            _fail("INVALID_PASS", "Pass 0 must be the candidate pass", "$.pass_kind")
        if body["sequence"] > 0 and body["pass_kind"] != "citation":
            _fail("INVALID_PASS", "Pass 1..N must be citation passes", "$.pass_kind")
        available = window["core_refs"] + window["context_refs"]
        for inspection in body["inspections"]:
            if not any(_covers(item, inspection["source_ref"]) for item in available):
                _fail("OUTSIDE_READING_PACKAGE", "inspection is not in the reading package", "$.inspections")
        refs = [*task["target_refs"], *[i["source_ref"] for i in body["inspections"]],
                *[r for c in body["candidates"] for r in c["support_refs"]]]
        units = self._live_units(refs, actor, package["document_registry_revision"])
        validate_references("knowledge_pass", body, units)
        _write_atomic(path, _json_bytes(body))
        owned_inspections = [item for item in body["inspections"]
                             if any(_covers(target, item["source_ref"]) for target in task["target_refs"])]
        task["inspections"] = _unique([*task["inspections"], *owned_inspections])
        task["revision"] += 1
        validate_references("work", task, units)
        _write_atomic(self._task_path(task_id), _json_bytes(task))
        return {"ok": True, "created": True, "pass": body, "task": task,
                "idempotency_key": body.get("idempotency_key"),
                "path": path.relative_to(self.vault).as_posix()}

    def record_pass_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Persist model-produced Pass records for a batch with per-task recovery."""
        self.source.enable_session_cache()
        batch_id = str(request["batch_id"])
        pass_requests = list(request["passes"])
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            allowed = set(batch["task_ids"])
            for item in pass_requests:
                if str(item.get("task_id", "")) not in allowed:
                    _fail("ACCESS_DENIED", "pass request is outside the batch", "$.passes")
            results, failures = [], []
            slices = self._slices(batch_id) if batch.get("slices_initialized") else []
            for item in pass_requests:
                task_id = str(item["task_id"])
                try:
                    matches = [value for value in slices
                               if task_id in value["task_ids"] and value["state"] != "cancelled"]
                    if len(matches) > 1:
                        _fail("STALE_INPUT", "task is assigned to multiple Pass slices")
                    selected = matches[0] if matches else None
                    requested_slice = item.get("slice_id", request.get("slice_id"))
                    if requested_slice is not None and (
                            selected is None or selected["slice_id"] != str(requested_slice)):
                        _fail("STALE_INPUT", "Pass request names a different slice")
                    requested_template = item.get("template_hash", request.get("template_hash"))
                    if selected is not None and requested_template is not None \
                            and str(requested_template) != selected["template_hash"]:
                        _fail("STALE_INPUT", "Pass template hash differs from the slice")
                    worker_id = item.get("worker_id", request.get("worker_id"))
                    if selected is not None and worker_id is not None and (
                            selected["state"] != "leased"
                            or selected["lease"]["worker_id"] != str(worker_id)):
                        _fail("ACCESS_DENIED", "Pass worker does not own the slice lease")
                    recorded = self.record_pass(
                        item, batch_id=batch_id,
                        template_hash=(selected["template_hash"] if selected is not None
                                       else str(requested_template or _pass_template_hash())))
                    results.append({"task_id": task_id, "created": recorded["created"],
                                    "pass_id": recorded["pass"]["pass_id"],
                                    "sequence": recorded["pass"]["sequence"],
                                    "idempotency_key": recorded["idempotency_key"],
                                    "path": recorded["path"],
                                    "task_revision": recorded["task"]["revision"]})
                except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                    failures.append(self._failure("pass", task_id, exc))
            updated = self._store_batch(batch, state="analyzing", operation="pass", failures=failures)
            return {"ok": not failures, "batch": updated, "results": results, "failures": failures}

    def _pass_index(self, task_ids: Iterable[str]) -> tuple[
            dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]],
            dict[str, list[dict[str, Any]]]]:
        result: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        by_task: dict[str, list[dict[str, Any]]] = {}
        for task_id in task_ids:
            root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/passes")
            for path in sorted(root.glob("*.json")) if root.exists() else []:
                record = _load_json(path)
                validate_record("knowledge_pass", record)
                if record["task_id"] != task_id:
                    _fail("IDENTITY_MISMATCH", "pass repository path and task_id disagree")
                by_task.setdefault(task_id, []).append(record)
                for candidate in record["candidates"]:
                    result[(record["pass_id"], candidate["candidate_id"])] = (record, candidate)
        return result, by_task

    def _decision_task_outputs(self, run: Mapping[str, Any]) -> dict[str, set[str]]:
        task_ids = [item["task_id"] for item in run["tasks"]]
        candidates, _ = self._pass_index(task_ids)
        outputs = {task_id: set() for task_id in task_ids}
        for decision in run["decisions"]:
            selected = []
            supporting_tasks = set()
            for candidate_ref in decision["candidate_refs"]:
                match = candidates.get((candidate_ref["pass_id"], candidate_ref["candidate_id"]))
                if match is None:
                    _fail("UNRESOLVED_REFERENCE", "build decision references a missing pass candidate")
                pass_record, candidate = match
                if pass_record["pass_kind"] != "citation":
                    _fail("INVALID_PASS", "build decision references Pass 0 instead of a citation pass")
                supporting_tasks.add(pass_record["task_id"])
                selected.append(candidate)
            expected_support = _unique(ref for candidate in selected for ref in candidate["support_refs"])
            if expected_support != decision["support_refs"]:
                _fail("PROVENANCE_MISMATCH", "build decision support differs from its pass candidates")
            for task_id in supporting_tasks:
                outputs[task_id].add(decision["path"])
        return outputs

    def reduce(self, request: Mapping[str, Any]) -> dict[str, Any]:
        run_id, actor = str(request["run_id"]), str(request["actor"])
        path = self._run_path(run_id)
        task_snapshots = list(request["tasks"])
        tasks = [self._task(item["task_id"]) for item in task_snapshots]
        for expected, task in zip(task_snapshots, tasks):
            if task["revision"] != expected["revision"] or task["status"] != "running":
                _fail("REVISION_CONFLICT", "Reduce requires current running task revisions", "$.tasks")
            if task["actor"] != actor:
                _fail("ACCESS_DENIED", "Reduce actor must own every task")
        registry = self._registry()
        if registry["revision"] != int(request["expected_registry_revision"]):
            _fail("REVISION_CONFLICT", "knowledge identity registry changed", "$.expected_registry_revision")
        candidates, passes_by_task = self._pass_index(item["task_id"] for item in task_snapshots)
        task_units = self._live_units(
            [ref for task in tasks for ref in [*task["target_refs"],
             *[item["source_ref"] for item in task["inspections"]],
             *[item["source_ref"] for item in task["deferred"]]]],
            actor, int(request["document_registry_revision"]))
        for task in tasks:
            passes = passes_by_task.get(task["task_id"], [])
            if not passes or passes[0]["sequence"] != 0 or passes[0]["pass_kind"] != "candidate":
                _fail("INCOMPLETE_PASSES", "Reduce requires a recorded Pass 0", "$.tasks")
            if [item["sequence"] for item in passes] != list(range(len(passes))):
                _fail("INCOMPLETE_PASSES", "knowledge pass sequence has a gap", "$.tasks")
            completion_probe = {**task, "status": "completed", "deferred": [],
                                "outputs": ["_system/knowledge-builds/pending.md"],
                                "reason": "coverage probe"}
            if task["deferred"]:
                _fail("INCOMPLETE_COVERAGE", "Reduce cannot consume a task with deferred target ranges")
            if not task["inspections"]:
                _fail("INCOMPLETE_COVERAGE", "context inspection does not cover the task target")
            validate_references("work", completion_probe, task_units)
        subjects = {item["subject_id"]: item for item in registry["subjects"]}
        paths = {item["current_path"]: item["subject_id"] for item in registry["subjects"]
                 if item["status"] == "active"}
        decisions, page_drafts = [], []
        used_paths: set[str] = set()
        all_refs: list[dict[str, Any]] = []
        for index, raw in enumerate(request["decisions"]):
            selected = []
            selected_passes = []
            for ref in raw["candidate_refs"]:
                match = candidates.get((ref["pass_id"], ref["candidate_id"]))
                if match is None:
                    _fail("UNRESOLVED_REFERENCE", "Reduce references an unknown pass candidate", f"$.decisions[{index}]")
                pass_record, candidate = match
                if pass_record["pass_kind"] != "citation":
                    _fail("INVALID_PASS", "Reduce support must come from Pass 1..N citation records",
                          f"$.decisions[{index}].candidate_refs")
                selected_passes.append(pass_record)
                selected.append(candidate)
            support = _unique(r for item in selected for r in item["support_refs"])
            identity = dict(raw["identity"])
            calculated_subject = fingerprint({"vault_id": self.source.vault_id, "kind": identity["kind"],
                                              "identity_key": identity["identity_key"]})
            subject_id = identity.get("subject_id") or calculated_subject
            if subject_id != calculated_subject:
                _fail("IDENTITY_MISMATCH", "subject_id does not match stable identity key", f"$.decisions[{index}].identity")
            existing = subjects.get(subject_id)
            if existing and (existing["kind"] != identity["kind"] or existing["identity_key"] != identity["identity_key"]):
                _fail("IDENTITY_MISMATCH", "existing subject has different identity semantics")
            action = str(raw["action"])
            if (existing is None) != (action == "create"):
                _fail("INVALID_DECISION", "create requires a new subject; other actions require an existing subject",
                      f"$.decisions[{index}].action")
            page_id = fingerprint({"vault_id": self.source.vault_id, "subject_id": subject_id})
            output_path = str(raw["path"])
            if output_path in used_paths:
                _fail("DUPLICATE", "two Reduce decisions target the same path", "$.decisions")
            used_paths.add(output_path)
            occupied = paths.get(output_path)
            if occupied is not None and occupied != subject_id:
                _fail("DUPLICATE", "output path belongs to another stable subject", f"$.decisions[{index}].path")
            target = _vault_path(self.vault, output_path)
            if output_path.split("/")[0] not in ("20_Notes", "30_Cards", "40_Concepts", "50_Projects") or target.suffix.lower() != ".md":
                _fail("INVALID_OUTPUT_PATH", "Reduce output must be governed Markdown", f"$.decisions[{index}].path")
            content = str(raw["content"]).replace("\r\n", "\n").replace("\r", "\n")
            if not content.strip():
                _fail("INVALID_SCHEMA", "page content cannot be empty", f"$.decisions[{index}].content")
            content = content.rstrip() + "\n"
            authored = _sha(content.encode("utf-8"))
            parent_revision = existing.get("current_revision_id") if existing else None
            qa_required = any(
                inspection["qa"] == "needs-qa" and any(_overlaps(inspection["source_ref"], ref) for ref in support)
                for pass_record in selected_passes for inspection in pass_record["inspections"])
            page = {"contract": "hermes-knowledge-page-revision/v1",
                    "page_id": page_id, "subject_id": subject_id, "parent_revision_id": parent_revision,
                    "action": action, "path": output_path, "authored_sha256": authored,
                    "support_refs": support, "qa_status": "qa_required" if qa_required else "usable",
                    "business_status": "unassessed", "visibility": "draft",
                    "state": "draft", "review": None}
            page["revision_id"] = fingerprint({key: page[key] for key in (
                "page_id", "subject_id", "parent_revision_id", "action", "path",
                "authored_sha256", "support_refs", "qa_status", "business_status", "visibility")})
            validate_record("page_revision", page)
            decisions.append({"candidate_refs": list(raw["candidate_refs"]), "subject_id": subject_id,
                              "page_id": page_id, "action": action, "path": output_path,
                              "support_refs": support, "revision_id": page["revision_id"],
                              "identity": {**identity, "subject_id": subject_id}})
            page_drafts.append((page, content))
            all_refs.extend(support)
        units = self._live_units(all_refs, actor, int(request["document_registry_revision"]))
        record = {"contract": "hermes-knowledge-build-run/v1", "run_id": run_id,
                  "revision": 1, "state": "draft", "actor": actor,
                  "identity_registry_revision": registry["revision"], "tasks": task_snapshots,
                  "decisions": decisions, "page_revisions": [item[0] for item in page_drafts],
                  "reviews": [], "reason": str(request.get("reason", ""))}
        validate_references("build_run", record, units)
        if path.exists():
            existing = _load_json(path)
            if existing != record:
                _fail("DUPLICATE", "run_id already identifies a different Reduce result", "$.run_id")
            return {"ok": True, "created": False, "run": existing}
        for page, content in page_drafts:
            draft = _vault_path(self.vault, f"{BUILD_ROOT}/{run_id}/pages/{page['page_id']}.md")
            _write_atomic(draft, content.encode("utf-8"))
        _write_atomic(path, _json_bytes(record))
        return {"ok": True, "created": True, "run": record}

    @staticmethod
    def _candidate_ref_key(value: Mapping[str, Any]) -> tuple[str, str]:
        return str(value["pass_id"]), str(value["candidate_id"])

    def reduce_resource(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one resource-local proposal set without reading raw packages."""
        batch_id = str(request["batch_id"])
        with _exclusive_lock(self._global_reduction_lock_path(batch_id)):
            return self._reduce_resource_locked(request)

    def _reduce_resource_locked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.source.enable_session_cache()
        batch_id, resource_id = str(request["batch_id"]), str(request["resource_id"])
        actor = str(request["actor"])
        snapshots = [dict(item) for item in request["tasks"]]
        task_ids = [str(item["task_id"]) for item in snapshots]
        if not task_ids or len(set(task_ids)) != len(task_ids):
            _fail("INVALID_SCHEMA", "resource reducer requires unique tasks", "$.tasks")
        template_id = str(request.get("template_id", RESOURCE_REDUCE_TEMPLATE_ID))
        template_hash = str(request.get("template_hash") or _reducer_template_hash(template_id))
        if not re.fullmatch(r"[0-9a-f]{64}", template_hash):
            _fail("INVALID_SCHEMA", "resource reducer template hash must be sha256 hex")

        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "resource reducer actor must own the batch")
            if not set(task_ids).issubset(set(batch["task_ids"])):
                _fail("ACCESS_DENIED", "resource reduction contains tasks outside the batch")
            tasks = [self._task(task_id) for task_id in task_ids]
            for snapshot, task in zip(snapshots, tasks):
                if task["actor"] != actor:
                    _fail("ACCESS_DENIED", "resource reducer actor must own every task")
                resources = {str(ref["unit_ref"]["resource_id"]) for ref in task["target_refs"]}
                if resources != {resource_id}:
                    _fail("INVALID_SCOPE", "resource reducer task crosses resource boundaries")

            candidates, passes_by_task = self._pass_index(task_ids)
            pass_refs = []
            available: set[tuple[str, str]] = set()
            for task_id in task_ids:
                passes = passes_by_task.get(task_id, [])
                if not passes or passes[0]["sequence"] != 0 or passes[0]["pass_kind"] != "candidate":
                    _fail("INCOMPLETE_PASSES", "resource reducer requires Pass 0 for every task")
                if [item["sequence"] for item in passes] != list(range(len(passes))):
                    _fail("INCOMPLETE_PASSES", "resource reducer found a Pass sequence gap")
                for item in passes:
                    candidate_ids = [str(candidate["candidate_id"]) for candidate in item["candidates"]]
                    pass_refs.append({"pass_id": item["pass_id"], "task_id": task_id,
                                      "sequence": item["sequence"],
                                      "pass_kind": item["pass_kind"],
                                      "candidate_ids": candidate_ids})
                    if item["pass_kind"] == "citation":
                        available.update((item["pass_id"], candidate_id)
                                         for candidate_id in candidate_ids)

            proposals = [dict(item) for item in request.get("proposals", [])]
            proposal_ids = [str(item.get("proposal_id", "")) for item in proposals]
            if len(set(proposal_ids)) != len(proposal_ids):
                _fail("DUPLICATE", "resource reduction has duplicate proposal ids")
            proposed: set[tuple[str, str]] = set()
            for proposal in proposals:
                for candidate_ref in proposal["candidate_refs"]:
                    key = self._candidate_ref_key(candidate_ref)
                    if key not in available or key not in candidates:
                        _fail("UNRESOLVED_REFERENCE",
                              "resource proposal references a non-citation candidate")
                    if key in proposed:
                        _fail("DUPLICATE", "citation candidate appears in multiple proposals")
                    proposed.add(key)
            omitted_refs = [dict(item) for item in request.get("omitted_candidate_refs", [])]
            omitted = {self._candidate_ref_key(item) for item in omitted_refs}
            if len(omitted) != len(omitted_refs) or proposed & omitted:
                _fail("DUPLICATE", "candidate cannot be proposed and omitted")
            if proposed | omitted != available:
                _fail("INCOMPLETE_COVERAGE",
                      "resource reduction must propose or explicitly omit every citation candidate")

            input_fingerprint = "sha256:" + fingerprint({
                "batch_id": batch_id, "resource_id": resource_id,
                "tasks": snapshots, "passes": pass_refs,
                "template_id": template_id, "template_hash": template_hash,
            })
            reduction_id = fingerprint({"batch_id": batch_id, "resource_id": resource_id,
                                        "input_fingerprint": input_fingerprint,
                                        "template_hash": template_hash})
            output_fingerprint = "sha256:" + fingerprint({
                "proposals": proposals, "omitted_candidate_refs": omitted_refs,
                "reason": str(request.get("reason", "")),
            })
            record = {
                "contract": "hermes-resource-reduction/v1", "reduction_id": reduction_id,
                "idempotency_key": "sha256:" + reduction_id,
                "batch_id": batch_id, "resource_id": resource_id, "actor": actor,
                "tasks": snapshots, "pass_refs": pass_refs,
                "input_fingerprint": input_fingerprint,
                "template_id": template_id, "template_hash": template_hash,
                "proposals": proposals, "omitted_candidate_refs": omitted_refs,
                "output_fingerprint": output_fingerprint,
                "state": "completed", "revision": 1,
                "reason": str(request.get("reason", "")),
            }
            validate_record("resource_reduction", record)
            for existing_id in batch.get("resource_reduction_ids", []):
                existing_record = self._resource_reduction(batch_id, existing_id)
                if (existing_record["resource_id"] == resource_id
                        and existing_id != reduction_id):
                    _fail("STALE_INPUT",
                          "resource already has a reduction pinned to different Pass inputs")
            path = self._resource_reduction_path(batch_id, reduction_id)
            created = not path.exists()
            if not created and _load_json(path) != record:
                _fail("IDEMPOTENCY_CONFLICT",
                      "same resource reduction input has different output")
            if not created and reduction_id in batch.get("resource_reduction_ids", []):
                return {"ok": True, "created": False, "reduction": record,
                        "batch": batch}
            if batch.get("cancel_requested"):
                _fail("CANCELLED", "resource reduction rejected after batch cancellation")
            if batch.get("global_reduction_id") is not None:
                _fail("INVALID_STATE", "resource reductions are closed after global coordination")
            for snapshot, task in zip(snapshots, tasks):
                if task["revision"] != int(snapshot["revision"]) or task["status"] != "running":
                    _fail("STALE_INPUT", "resource reducer requires current running task revisions")
            if created:
                _write_atomic(path, _json_bytes(record))
            updated_batch = copy.deepcopy(batch)
            updated_batch.update({
                "state": "analyzing", "last_operation": "resource_reduce",
                "resource_reduction_ids": sorted(set(
                    [*batch.get("resource_reduction_ids", []), reduction_id])),
                "reduction_config": dict(batch.get("reduction_config") or DEFAULT_REDUCTION_CONFIG),
                "revision": int(batch["revision"]) + 1,
            })
            validate_record("knowledge_batch", updated_batch)
            _write_atomic(self._batch_path(batch_id), _json_bytes(updated_batch))
            return {"ok": True, "created": created, "reduction": record,
                    "batch": updated_batch}

    def reduce_global(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Coordinate resource proposals once, then materialize ordinary draft runs."""
        self.source.enable_session_cache()
        batch_id, actor = str(request["batch_id"]), str(request["actor"])
        reduction_ids = sorted(str(item) for item in request["resource_reduction_ids"])
        if not reduction_ids or len(set(reduction_ids)) != len(reduction_ids):
            _fail("INVALID_SCHEMA", "global reducer requires unique resource reductions")
        template_id = str(request.get("template_id", GLOBAL_REDUCE_TEMPLATE_ID))
        template_hash = str(request.get("template_hash") or _reducer_template_hash(template_id))
        if not re.fullmatch(r"[0-9a-f]{64}", template_hash):
            _fail("INVALID_SCHEMA", "global reducer template hash must be sha256 hex")
        runs = [dict(item) for item in request["runs"]]
        omitted_refs = [dict(item) for item in request.get("omitted_candidate_refs", [])]
        reductions = [self._resource_reduction(batch_id, item) for item in reduction_ids]
        input_fingerprint = "sha256:" + fingerprint({
            "batch_id": batch_id,
            "resource_reductions": [{"reduction_id": item["reduction_id"],
                                     "output_fingerprint": item["output_fingerprint"]}
                                    for item in reductions],
            "template_id": template_id, "template_hash": template_hash,
        })
        coordination_id = fingerprint({"batch_id": batch_id,
                                       "input_fingerprint": input_fingerprint,
                                       "template_hash": template_hash})
        output_fingerprint = "sha256:" + fingerprint({
            "runs": runs, "omitted_candidate_refs": omitted_refs,
            "reason": str(request.get("reason", "")),
        })
        record = {
            "contract": "hermes-global-reduction/v1",
            "coordination_id": coordination_id,
            "idempotency_key": "sha256:" + coordination_id,
            "batch_id": batch_id, "actor": actor,
            "resource_reduction_ids": reduction_ids,
            "input_fingerprint": input_fingerprint,
            "template_id": template_id, "template_hash": template_hash,
            "run_ids": [str(item["run_id"]) for item in runs],
            "omitted_candidate_refs": omitted_refs,
            "output_fingerprint": output_fingerprint,
            "state": "completed", "revision": 1,
            "reason": str(request.get("reason", "")),
        }
        validate_record("global_reduction", record)

        with _exclusive_lock(self._global_reduction_lock_path(batch_id)):
            path = self._global_reduction_path(batch_id, coordination_id)
            if path.exists():
                existing = self._global_reduction(batch_id, coordination_id)
                if existing != record:
                    _fail("IDEMPOTENCY_CONFLICT",
                          "same global reduction input has different output")
                with _exclusive_lock(self._batch_lock_path(batch_id)):
                    current = self._batch(batch_id)
                    existing_global = current.get("global_reduction_id")
                    if existing_global not in (None, coordination_id):
                        _fail("IDEMPOTENCY_CONFLICT",
                              "batch already has a different global reduction")
                    if existing_global is None:
                        current = copy.deepcopy(current)
                        current.update({"global_reduction_id": coordination_id,
                                        "reduction_config": dict(
                                            current.get("reduction_config")
                                            or DEFAULT_REDUCTION_CONFIG),
                                        "last_operation": "global_reduce",
                                        "revision": int(current["revision"]) + 1})
                        validate_record("knowledge_batch", current)
                        _write_atomic(self._batch_path(batch_id), _json_bytes(current))
                return {"ok": True, "created": False, "coordination": existing,
                        "runs": [{"run_id": run_id, "created": False}
                                 for run_id in existing["run_ids"]], "batch": current}

            batch = self._batch(batch_id)
            if batch.get("cancel_requested"):
                _fail("CANCELLED", "global reduction rejected after batch cancellation")
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "global reducer actor must own the batch")
            configured = batch.get("reduction_config") or DEFAULT_REDUCTION_CONFIG
            if int(configured["global_reducer_concurrency"]) != 1:
                _fail("INVALID_SCHEMA", "global reducer concurrency must remain 1")
            if set(reduction_ids) != set(batch.get("resource_reduction_ids", [])):
                _fail("STALE_INPUT", "global reducer does not pin the complete resource reduction set")
            if batch.get("global_reduction_id") not in (None, coordination_id):
                _fail("IDEMPOTENCY_CONFLICT", "batch already has a different global reduction")
            if any(item["actor"] != actor for item in reductions):
                _fail("ACCESS_DENIED", "resource reduction actor differs from global actor")

            task_owners: dict[str, str] = {}
            proposed: set[tuple[str, str]] = set()
            for reduction in reductions:
                for snapshot in reduction["tasks"]:
                    task_id = str(snapshot["task_id"])
                    if task_id in task_owners:
                        _fail("DUPLICATE", "task appears in multiple resource reductions")
                    task = self._task(task_id)
                    if (task["revision"] != int(snapshot["revision"])
                            or task["status"] != "running"):
                        _fail("STALE_INPUT", "resource task revision changed before global Reduce")
                    current_passes = self._pass_index([task_id])[1].get(task_id, [])
                    pinned_passes = [item for item in reduction["pass_refs"]
                                     if item["task_id"] == task_id]
                    observed_passes = [{"pass_id": item["pass_id"], "task_id": task_id,
                                        "sequence": item["sequence"],
                                        "pass_kind": item["pass_kind"],
                                        "candidate_ids": [str(candidate["candidate_id"])
                                                          for candidate in item["candidates"]]}
                                       for item in current_passes]
                    if pinned_passes != observed_passes:
                        _fail("STALE_INPUT", "resource Pass set changed before global Reduce")
                    task_owners[task_id] = reduction["reduction_id"]
                for proposal in reduction["proposals"]:
                    proposed.update(self._candidate_ref_key(item)
                                    for item in proposal["candidate_refs"])
            eligible = {task_id for task_id in batch["task_ids"]
                        if self._task(task_id)["status"] not in ("blocked", "failed", "skipped")}
            if set(task_owners) != eligible:
                _fail("INCOMPLETE_COVERAGE",
                      "resource reductions must cover every eligible batch task exactly once")

            run_task_owners: dict[str, str] = {}
            used_refs: list[tuple[str, str]] = []
            for run in runs:
                run_id = str(run["run_id"])
                for snapshot in run["tasks"]:
                    task_id = str(snapshot["task_id"])
                    if task_id in run_task_owners:
                        _fail("DUPLICATE", "task is assigned to multiple draft runs")
                    run_task_owners[task_id] = run_id
                for decision in run["decisions"]:
                    used_refs.extend(self._candidate_ref_key(item)
                                     for item in decision["candidate_refs"])
            if set(run_task_owners) != eligible:
                _fail("INCOMPLETE_COVERAGE",
                      "global reducer must assign every eligible task to one draft run")
            if len(set(used_refs)) != len(used_refs):
                _fail("DUPLICATE", "citation candidate is assigned to multiple decisions")
            used = set(used_refs)
            omitted = {self._candidate_ref_key(item) for item in omitted_refs}
            if len(omitted) != len(omitted_refs) or used & omitted:
                _fail("DUPLICATE", "global candidate cannot be used and omitted")
            if used | omitted != proposed:
                _fail("INCOMPLETE_COVERAGE",
                      "global reducer must use or omit every resource proposal candidate")

            reduced = self.reduce_batch({"batch_id": batch_id, "runs": runs})
            if not reduced["ok"]:
                return {"ok": False, "created": False, "coordination": None,
                        "runs": reduced["results"], "failures": reduced["failures"]}
            _write_atomic(path, _json_bytes(record))
            with _exclusive_lock(self._batch_lock_path(batch_id)):
                current = self._batch(batch_id)
                existing_global = current.get("global_reduction_id")
                if existing_global not in (None, coordination_id):
                    _fail("IDEMPOTENCY_CONFLICT", "batch already has a different global reduction")
                current = copy.deepcopy(current)
                current.update({"global_reduction_id": coordination_id,
                                "reduction_config": dict(configured),
                                "last_operation": "global_reduce",
                                "revision": int(current["revision"]) + 1})
                validate_record("knowledge_batch", current)
                _write_atomic(self._batch_path(batch_id), _json_bytes(current))
            return {"ok": True, "created": True, "coordination": record,
                    "runs": reduced["results"], "batch": current}

    def reduce_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Create non-conflicting draft runs while retaining legacy run artifacts."""
        self.source.enable_session_cache()
        batch_id = str(request["batch_id"])
        reduce_requests = list(request["runs"])
        run_ids = [str(item["run_id"]) for item in reduce_requests]
        if len(set(run_ids)) != len(run_ids):
            _fail("DUPLICATE", "batch reduce contains duplicate run ids", "$.runs")
        used_tasks: dict[str, str] = {}
        used_paths: dict[str, str] = {}
        used_identities: dict[tuple[str, str], str] = {}
        for item in reduce_requests:
            run_id = str(item["run_id"])
            for snapshot in item["tasks"]:
                task_id = str(snapshot["task_id"])
                if task_id in used_tasks:
                    _fail("DUPLICATE", f"task {task_id} is assigned to runs {used_tasks[task_id]} and {run_id}",
                          "$.runs")
                used_tasks[task_id] = run_id
            for decision in item["decisions"]:
                path = str(decision["path"])
                identity = decision["identity"]
                key = (str(identity["kind"]), str(identity["identity_key"]))
                if path in used_paths:
                    _fail("DUPLICATE", f"path {path} is proposed by multiple runs", "$.runs")
                if key in used_identities:
                    _fail("DUPLICATE", "one stable identity is proposed by multiple runs; merge it before Reduce",
                          "$.runs")
                used_paths[path], used_identities[key] = run_id, run_id
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            allowed = set(batch["task_ids"])
            if not set(used_tasks).issubset(allowed):
                _fail("ACCESS_DENIED", "Reduce request contains tasks outside the batch", "$.runs")
            results, failures = [], []
            successful = list(batch["run_ids"])
            for item in reduce_requests:
                run_id = str(item["run_id"])
                try:
                    reduced = self.reduce(item)
                    successful.append(run_id)
                    results.append({"run_id": run_id, "created": reduced["created"],
                                    "revision": reduced["run"]["revision"],
                                    "page_count": len(reduced["run"]["page_revisions"])})
                except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                    failures.append(self._failure("reduce", run_id, exc))
            updated = self._store_batch(batch, state="analyzing", operation="reduce",
                                        run_ids=successful, failures=failures)
            return {"ok": not failures, "batch": updated, "results": results, "failures": failures}

    def finalize(self, request: Mapping[str, Any]) -> dict[str, Any]:
        lock = _vault_path(self.vault, f"{BUILD_ROOT}/.finalize.lock")
        with _exclusive_lock(lock):
            return self._finalize_locked(request)

    def _finalize_locked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        run_id, actor = str(request["run_id"]), str(request["actor"])
        path = self._run_path(run_id)
        run = _load_json(path)
        validate_record("build_run", run)
        if run["state"] == "completed":
            return {"ok": True, "committed": False, "run": run}
        if run["revision"] != int(request["expected_revision"]):
            _fail("REVISION_CONFLICT", "build revision changed", "$.expected_revision")
        if run["actor"] != actor:
            _fail("ACCESS_DENIED", "Finalize actor must own the build")
        reviews = list(request["reviews"])
        by_page = {item["page_id"]: item for item in reviews}
        if len(by_page) != len(reviews) or set(by_page) != {item["page_id"] for item in run["page_revisions"]}:
            _fail("REVIEW_REQUIRED", "every page revision needs exactly one review", "$.reviews")
        registry = self._registry()
        proposed_registry = copy.deepcopy(registry)
        subject_by_id = {item["subject_id"]: item for item in proposed_registry["subjects"]}
        path_owners = {item["current_path"]: item["subject_id"] for item in proposed_registry["subjects"]
                       if item["status"] == "active"}
        decisions = {item["page_id"]: item for item in run["decisions"]}
        outputs_by_task = self._decision_task_outputs(run)
        page_writes: list[tuple[Path, bytes]] = []
        revision_writes: list[tuple[Path, dict[str, Any]]] = []
        committed_pages: list[dict[str, Any]] = []
        registry_changed = False
        for page in run["page_revisions"]:
            review = by_page[page["page_id"]]
            draft = _vault_path(self.vault, f"{BUILD_ROOT}/{run_id}/pages/{page['page_id']}.md")
            content = draft.read_bytes()
            if _sha(content) != page["authored_sha256"] or review["authored_sha256"] != page["authored_sha256"]:
                _fail("SOURCE_CHANGED", "reviewed page draft hash differs")
            if not str(review.get("note", "")).strip() or not str(review.get("actor", "")).strip():
                _fail("REVIEW_REQUIRED", "review actor and note are required")
            decision = decisions[page["page_id"]]
            identity = decision["identity"]
            subject = subject_by_id.get(page["subject_id"])
            desired = {"subject_id": page["subject_id"], "page_id": page["page_id"],
                       "kind": identity["kind"], "identity_key": identity["identity_key"],
                       "canonical_name": identity["canonical_name"], "aliases": identity["aliases"],
                       "status": "active", "current_path": page["path"],
                       "current_revision_id": page["revision_id"],
                       "created_by_run": subject["created_by_run"] if subject else run_id}
            partial = subject is not None and subject == desired
            if page["parent_revision_id"] is None:
                if review.get("parent_authored_sha256") is not None:
                    _fail("REVIEW_REQUIRED", "create review cannot claim a parent page")
                if subject is not None and not partial:
                    _fail("REVISION_CONFLICT", "new subject identity was claimed after Reduce")
            else:
                if subject is None:
                    _fail("REVISION_CONFLICT", "existing subject disappeared after Reduce")
                if subject["current_revision_id"] not in (page["parent_revision_id"], page["revision_id"]):
                    _fail("REVISION_CONFLICT", "subject page changed after Reduce")
                if review.get("parent_authored_sha256") is None:
                    _fail("REVIEW_REQUIRED", "non-create review must pin the parent page hash")
                if subject["current_revision_id"] == page["parent_revision_id"]:
                    parent = _vault_path(self.vault, subject["current_path"])
                    if not parent.is_file() or _sha(parent.read_bytes()) != review["parent_authored_sha256"]:
                        _fail("SOURCE_CHANGED", "parent page changed after Reduce", "$.reviews")
                else:
                    parent_record = _vault_path(
                        self.vault, f"{BUILD_ROOT}/page-revisions/{page['page_id']}/{page['parent_revision_id']}.json")
                    if (not parent_record.is_file()
                            or _load_json(parent_record)["authored_sha256"] != review["parent_authored_sha256"]):
                        _fail("SOURCE_CHANGED", "parent revision cannot be verified during recovery")
            owner = path_owners.get(page["path"])
            if owner is not None and owner != page["subject_id"]:
                _fail("DUPLICATE", "knowledge path is owned by another subject")
            target = _vault_path(self.vault, page["path"])
            if target.exists():
                target_hash = _sha(target.read_bytes())
                allowed = {page["authored_sha256"]}
                if subject is not None and target == _vault_path(self.vault, subject["current_path"]):
                    allowed.add(review.get("parent_authored_sha256"))
                if target_hash not in allowed:
                    _fail("SOURCE_CHANGED", "target page changed after Reduce", "$.reviews")
            if not target.exists() or _sha(target.read_bytes()) != page["authored_sha256"]:
                page_writes.append((target, content))
            committed = {**page, "state": "committed",
                         "review": {"actor": review["actor"], "note": review["note"]}}
            validate_record("page_revision", committed)
            committed_pages.append(committed)
            revision_path = _vault_path(self.vault, f"{BUILD_ROOT}/page-revisions/{page['page_id']}/{page['revision_id']}.json")
            if revision_path.exists() and _load_json(revision_path) != committed:
                _fail("SOURCE_CHANGED", "page revision identity collision")
            if not revision_path.exists():
                revision_writes.append((revision_path, committed))
            if not partial:
                if subject:
                    subject.update(desired)
                else:
                    proposed_registry["subjects"].append(desired)
                    subject_by_id[page["subject_id"]] = desired
                path_owners[page["path"]] = page["subject_id"]
                registry_changed = True
        if registry_changed:
            proposed_registry["revision"] += 1
        validate_record("identity_registry", proposed_registry)
        proposed_tasks: list[tuple[Path, dict[str, Any]]] = []
        task_refs: list[dict[str, Any]] = []
        for snapshot in run["tasks"]:
            task = self._task(snapshot["task_id"])
            if task["revision"] == snapshot["revision"]:
                task.update({"status": "completed", "revision": task["revision"] + 1,
                             "outputs": sorted(outputs_by_task[task["task_id"]]),
                             "reason": f"completed by build {run_id}"})
                task_refs.extend([*task["target_refs"], *[item["source_ref"] for item in task["inspections"]]])
                proposed_tasks.append((self._task_path(task["task_id"]), task))
            elif not (task["status"] == "completed" and task["revision"] == snapshot["revision"] + 1
                      and task["reason"] == f"completed by build {run_id}"):
                _fail("REVISION_CONFLICT", "task changed during Build Finalize")
        task_units = self._live_units(task_refs, actor, self._document_registry_revision()) if task_refs else []
        for _, task in proposed_tasks:
            validate_references("work", task, task_units)
        completed_run = {**run, "state": "completed", "revision": run["revision"] + 1,
                         "reviews": reviews, "page_revisions": committed_pages}
        support_refs = [ref for page in committed_pages for ref in page["support_refs"]]
        support_units = self._live_units(
            support_refs, actor, self._document_registry_revision()) if support_refs else []
        validate_references("build_run", completed_run, support_units)

        for target, content in page_writes:
            _write_atomic(target, content)
        for revision_path, committed in revision_writes:
            _write_atomic(revision_path, _json_bytes(committed))
        if registry_changed:
            _write_atomic(_vault_path(self.vault, IDENTITY_PATH), _json_bytes(proposed_registry))
        for task_path, task in proposed_tasks:
            _write_atomic(task_path, _json_bytes(task))
        _write_atomic(path, _json_bytes(completed_run))
        return {"ok": True, "committed": True, "run": completed_run,
                "identity_registry_revision": proposed_registry["revision"]}

    def validate_run(self, run_id: str) -> dict[str, Any]:
        run = _load_json(self._run_path(run_id))
        validate_record("build_run", run)
        self._decision_task_outputs(run)
        refs = [ref for page in run["page_revisions"] for ref in page["support_refs"]]
        units = self._live_units(refs, run["actor"], self._document_registry_revision()) if refs else []
        validate_references("build_run", run, units)
        registry = self._registry()
        subjects = {item["page_id"]: item for item in registry["subjects"]}
        for page in run["page_revisions"]:
            draft = _vault_path(self.vault, f"{BUILD_ROOT}/{run_id}/pages/{page['page_id']}.md")
            if _sha(draft.read_bytes()) != page["authored_sha256"]:
                _fail("SOURCE_CHANGED", "build page draft changed")
            if run["state"] == "completed":
                revision_path = _vault_path(
                    self.vault, f"{BUILD_ROOT}/page-revisions/{page['page_id']}/{page['revision_id']}.json")
                if _load_json(revision_path) != page:
                    _fail("SOURCE_CHANGED", "committed page revision sidecar changed")
                subject = subjects.get(page["page_id"])
                if subject is None or subject["subject_id"] != page["subject_id"]:
                    _fail("IDENTITY_MISMATCH", "identity registry no longer contains the committed page identity")
                if subject["current_revision_id"] == page["revision_id"]:
                    target = _vault_path(self.vault, subject["current_path"])
                    if _sha(target.read_bytes()) != page["authored_sha256"]:
                        _fail("SOURCE_CHANGED", "current committed knowledge page changed")
        return {"ok": True, "run_id": run_id, "state": run["state"],
                "revision": run["revision"], "page_count": len(run["page_revisions"])}

    def validate_batch(self, batch_id: str) -> dict[str, Any]:
        """Validate draft/completed runs and enforce batch task coverage."""
        self.source.enable_session_cache()
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            results, failures = [], []
            covered: dict[str, list[str]] = {}
            for run_id in batch["run_ids"]:
                try:
                    result = self.validate_run(run_id)
                    run = _load_json(self._run_path(run_id))
                    snapshot_ids = []
                    for snapshot in run["tasks"]:
                        task = self._task(snapshot["task_id"])
                        if run["state"] == "draft":
                            if (task["revision"] != snapshot["revision"] or task["status"] != "running"
                                    or task["actor"] != run["actor"]):
                                _fail("REVISION_CONFLICT", "draft run task changed after Reduce")
                        elif not (task["status"] == "completed"
                                  and task["revision"] == snapshot["revision"] + 1
                                  and task["reason"] == f"completed by build {run_id}"):
                            _fail("REVISION_CONFLICT", "completed run task state no longer matches its snapshot")
                        snapshot_ids.append(snapshot["task_id"])
                    for task_id in snapshot_ids:
                        covered.setdefault(task_id, []).append(run_id)
                    results.append(result)
                except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                    failures.append(self._failure("validate", run_id, exc))
            duplicate = sorted(task_id for task_id, values in covered.items() if len(values) > 1)
            tasks = [self._task(task_id) for task_id in batch["task_ids"]]
            outside = sorted(task_id for task_id in covered if task_id not in set(batch["task_ids"]))
            uncovered = sorted(task["task_id"] for task in tasks
                               if task["status"] not in ("blocked", "failed", "skipped")
                               and task["task_id"] not in covered)
            ready = not failures and not duplicate and not outside and not uncovered
            state = "checkpoint_1" if ready else "analyzing"
            updated = self._store_batch(batch, state=state, operation="validate", failures=failures)
            return {"ok": ready, "batch": updated, "runs": results, "failures": failures,
                    "uncovered_task_ids": uncovered, "duplicate_task_run_ids": duplicate,
                    "outside_batch_task_ids": outside,
                    "terminal_exceptions": sorted(task["task_id"] for task in tasks
                                                  if task["status"] in ("blocked", "failed", "skipped"))}

    def finalize_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Finalize approved runs under one lock, committing each legacy run idempotently."""
        self.source.enable_session_cache()
        batch_id, actor = str(request["batch_id"]), str(request["actor"])
        finalizations = list(request["finalizations"])
        run_ids = [str(item["run_id"]) for item in finalizations]
        if len(set(run_ids)) != len(run_ids):
            _fail("DUPLICATE", "batch finalize contains duplicate run ids", "$.finalizations")
        with _exclusive_lock(self._batch_lock_path(batch_id)):
            batch = self._batch(batch_id)
            if batch["actor"] != actor:
                _fail("ACCESS_DENIED", "batch finalize actor does not own the batch")
            if not set(run_ids).issubset(set(batch["run_ids"])):
                _fail("ACCESS_DENIED", "batch finalize contains a run outside the batch")

            used_subjects: dict[str, str] = {}
            used_paths: dict[str, str] = {}
            for item in finalizations:
                run_id = str(item["run_id"])
                run = _load_json(self._run_path(run_id))
                validate_record("build_run", run)
                if run["state"] == "completed":
                    continue
                if run["actor"] != actor or run["revision"] != int(item["expected_revision"]):
                    _fail("REVISION_CONFLICT", "approved run actor or revision changed", "$.finalizations")
                reviews = list(item["reviews"])
                by_page = {review["page_id"]: review for review in reviews}
                pages = {page["page_id"]: page for page in run["page_revisions"]}
                if len(by_page) != len(reviews) or set(by_page) != set(pages):
                    _fail("REVIEW_REQUIRED", "every approved page needs exactly one review", "$.finalizations")
                for page_id, page in pages.items():
                    review = by_page[page_id]
                    if review["authored_sha256"] != page["authored_sha256"]:
                        _fail("SOURCE_CHANGED", "approved draft hash changed", "$.finalizations")
                    if page["subject_id"] in used_subjects:
                        _fail("DUPLICATE", "one subject is finalized by multiple runs in the batch",
                              "$.finalizations")
                    if page["path"] in used_paths:
                        _fail("DUPLICATE", "one output path is finalized by multiple runs in the batch",
                              "$.finalizations")
                    used_subjects[page["subject_id"]] = run_id
                    used_paths[page["path"]] = run_id

            results, failures = [], []
            finalize_lock = _vault_path(self.vault, f"{BUILD_ROOT}/.finalize.lock")
            with _exclusive_lock(finalize_lock):
                for item in finalizations:
                    run_id = str(item["run_id"])
                    try:
                        finalized = self._finalize_locked(item)
                        results.append({"run_id": run_id, "committed": finalized["committed"],
                                        "revision": finalized["run"]["revision"],
                                        "page_count": len(finalized["run"]["page_revisions"])})
                    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                        failures.append(self._failure("finalize", run_id, exc))
                        break
            run_states = []
            for run_id in batch["run_ids"]:
                path = self._run_path(run_id)
                run_states.append(_load_json(path).get("state") if path.is_file() else "missing")
            all_runs_completed = bool(run_states) and all(state == "completed" for state in run_states)
            task_states = [self._task(task_id)["status"] for task_id in batch["task_ids"]]
            completed = all_runs_completed and all(state in ("completed", "skipped") for state in task_states)
            blocked = bool(failures) or any(state in ("blocked", "failed") for state in task_states)
            state = "completed" if completed else ("blocked" if blocked else "finalizing")
            updated = self._store_batch(batch, state=state, operation="finalize", failures=failures)
            return {"ok": not failures, "batch": updated, "results": results,
                    "failures": failures, "all_runs_completed": all_runs_completed,
                    "batch_completed": completed}

    def list_identities(self) -> dict[str, Any]:
        """Return exact identities for semantic comparison; never fuzzy-merge names."""
        return self._registry()

    def _document_registry_revision(self) -> int:
        governance = self.source.vault_manifest["governance"]
        registry = _load_json(_vault_path(self.vault, governance["repository"]["registry_path"]))
        return int(registry["registry_revision"])
