"""P3 file-backed Pass/Reduce knowledge construction and Build Finalize."""
from __future__ import annotations

import copy
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
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


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
                 "state": "planned", "last_operation": "plan", "failures": []}
        validate_record("knowledge_batch", batch)
        return batch

    def plan_batch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Create a complete task batch with one SourceUnit and overlap preflight."""
        self.source.enable_session_cache()
        batch_id, actor = str(request["batch_id"]), str(request["actor"])
        registry_revision = int(request["registry_revision"])
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

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        batch = self._batch(batch_id)
        tasks = [self._task(task_id) for task_id in batch["task_ids"]]
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
        if any(item["status"] == "pending" or (item["status"] == "running" and not item["reading_packages"])
               for item in task_details):
            next_actions.append("batch-prepare")
        if any(item["status"] == "running" and item["reading_packages"] and not item["passes"]
               for item in task_details):
            next_actions.append("batch-pass")
        if any(item["status"] == "running" and item["passes"] and item["task_id"] in uncovered
               for item in task_details):
            next_actions.append("batch-reduce")
        if batch["run_ids"]:
            next_actions.append("batch-validate")
        if any(item["state"] == "draft" for item in run_details):
            next_actions.append("human-checkpoint-1-then-batch-finalize")
        if run_details and all(item["state"] == "completed" for item in run_details):
            next_actions.append("vault-finalize-plan")
        return {"ok": True, "batch": batch, "task_counts": task_counts,
                "tasks": task_details, "runs": run_details,
                "uncovered_task_ids": uncovered,
                "duplicate_task_run_ids": duplicate_task_runs,
                "next_actions": list(dict.fromkeys(next_actions))}

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
                                    "total_codepoints": package["total_codepoints"]})
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
        maximum = int(max_codepoints or self.source.config["reading"]["max_codepoints"])
        result = self.source.context({"core_refs": task["target_refs"],
                                     "access": {"actor": actor, "purpose": "construction",
                                                "registry_revision": registry_revision},
                                     "max_codepoints": maximum})
        context_refs = [item["source_ref"] for item in result["context"]]
        payload = {"core_refs": task["target_refs"], "context_refs": context_refs,
                   "context_header": " | ".join(dict.fromkeys(
                       " / ".join(item.get("heading_path", []))
                       for item in [*result["core"], *result["context"]]
                       if item.get("heading_path"))),
                   "omitted_refs": result["omitted_refs"], "truncated": result["truncated"],
                   "reason": result["reason"], "max_codepoints": maximum}
        window = {"contract": "hermes-reading-window/v1",
                  "window_id": fingerprint(payload), **payload}
        validate_record("reading_window", window)
        materials = []
        for role, values in (("core", result["core"]), ("context", result["context"])):
            for item in values:
                selected = item["core_text"]
                materials.append({
                    "role": role, "source_ref": item["source_ref"],
                    "unit_content_sha256": item["content_sha256"],
                    "selected_sha256": _sha(selected.encode("utf-8")) if selected is not None else None,
                    "core_text": selected, "asset_refs": item["asset_refs"],
                    "quality_refs": item["quality_refs"], "section_id": item["section_id"],
                    "heading_path": item["heading_path"],
                })
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
                "package": package, "window": window,
                "core": result["core"], "context": result["context"]}

    def record_pass(self, request: Mapping[str, Any]) -> dict[str, Any]:
        task_id, actor = str(request["task_id"]), str(request["actor"])
        task = self._task(task_id)
        expected_revision = int(request["expected_revision"])
        package_id = str(request["reading_package_id"])
        package_path = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/readings/{package_id}.json")
        package = _load_json(package_path)
        validate_record("reading_package", package)
        window = package["window"]
        if window["core_refs"] != task["target_refs"]:
            _fail("TASK_WINDOW_MISMATCH", "reading window does not belong to current task")
        body = {"contract": "hermes-knowledge-pass/v1", "task_id": task_id,
                "task_revision": expected_revision, "pass_kind": str(request["pass_kind"]),
                "sequence": int(request["sequence"]), "reading_window_id": window["window_id"],
                "reading_package_id": package_id,
                "inspections": list(request["inspections"]), "candidates": list(request["candidates"]),
                "empty_reason": str(request.get("empty_reason", "")), "actor": actor}
        body["pass_id"] = fingerprint(body)
        validate_record("knowledge_pass", body)
        pass_root = _vault_path(self.vault, f"{BUILD_ROOT}/task-{task_id}/passes")
        path = pass_root / f"{body['sequence']:04d}-{body['pass_id']}.json"
        existing_sequence = list(pass_root.glob(f"{body['sequence']:04d}-*.json")) if pass_root.exists() else []
        if existing_sequence:
            if len(existing_sequence) == 1 and existing_sequence[0] == path and _load_json(path) == body:
                return {"ok": True, "created": False, "pass": body, "task": task,
                        "path": path.relative_to(self.vault).as_posix()}
            _fail("DUPLICATE", "pass sequence already contains different content", "$.sequence")
        if task["revision"] != expected_revision:
            _fail("REVISION_CONFLICT", "task revision changed", "$.expected_revision")
        if task["status"] != "running" or task["actor"] != actor:
            _fail("ACCESS_DENIED", "pass actor must own the running task")
        if package["task_id"] != task_id or package["actor"] != actor:
            _fail("TASK_WINDOW_MISMATCH", "reading package belongs to another task or actor")
        if int(request["registry_revision"]) != package["document_registry_revision"]:
            _fail("REVISION_CONFLICT", "pass must use the document registry revision observed by its reading package")
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
            for item in pass_requests:
                task_id = str(item["task_id"])
                try:
                    recorded = self.record_pass(item)
                    results.append({"task_id": task_id, "created": recorded["created"],
                                    "pass_id": recorded["pass"]["pass_id"],
                                    "sequence": recorded["pass"]["sequence"],
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
