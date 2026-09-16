"""P3 file-backed Pass/Reduce knowledge construction and Build Finalize."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterable, Mapping

from .source_units import (FileSourceUnitService, _json_bytes, _load_json,
                           _exclusive_lock, _sha, _vault_path, _write_atomic)
from .validation import (ContractError, canonical_json, fingerprint,
                         validate_record, validate_references)

WORK_ROOT = "_system/ledgers/unit-work"
BUILD_ROOT = "_system/knowledge-builds"
IDENTITY_PATH = "_system/metadata/knowledge-identities.json"
KNOWLEDGE_CONTRACT = "hermes-knowledge-build-run/v1"


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

    def _task(self, task_id: str) -> dict[str, Any]:
        value = _load_json(self._task_path(task_id))
        validate_record("work", value)
        return value

    def _registry(self) -> dict[str, Any]:
        value = _load_json(_vault_path(self.vault, IDENTITY_PATH))
        validate_record("identity_registry", value)
        return value

    def _live_units(self, refs: Iterable[Mapping[str, Any]], actor: str,
                    registry_revision: int) -> list[dict[str, Any]]:
        refs = list(refs)
        groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for ref in refs:
            unit = ref["unit_ref"]
            groups.setdefault((unit["resource_id"], unit["unit_set_id"]), []).append(ref)
            self.source.get({"source_ref": ref, "access": {"actor": actor,
                            "purpose": "construction", "registry_revision": registry_revision}})
        units: list[dict[str, Any]] = []
        for resource_id, unit_set_id in groups:
            self.source.validate(resource_id, unit_set_id)
            units.extend(self.source.list(resource_id, unit_set_id))
        return units

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

    def list_identities(self) -> dict[str, Any]:
        """Return exact identities for semantic comparison; never fuzzy-merge names."""
        return self._registry()

    def _document_registry_revision(self) -> int:
        governance = self.source.vault_manifest["governance"]
        registry = _load_json(_vault_path(self.vault, governance["repository"]["registry_path"]))
        return int(registry["registry_revision"])
