"""P4 incremental Vault Finalize and knowledge release publication."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_build import BUILD_ROOT, IDENTITY_PATH, FileKnowledgeBuildService
from .source_units import (UNIT_ROOT, FileSourceUnitService, _exclusive_lock,
                           _json_bytes, _load_json, _sha, _vault_path,
                           _write_atomic)
from .validation import ContractError, fingerprint, validate_record

RELEASE_ROOT = "_system/knowledge-releases"
RELEASE_STATE_PATH = "_system/metadata/knowledge-release-state.json"
NAVIGATION_PATH = "_system/navigation/knowledge.json"
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


def _unique_strings(values: Iterable[str]) -> list[str]:
    return sorted({str(value) for value in values if str(value)})


class FileVaultFinalizeService:
    """File-backed incremental Vault Finalize; it never invokes a Provider."""

    def __init__(self, vault_root: str | Path):
        self.source = FileSourceUnitService(vault_root)
        self.build = FileKnowledgeBuildService(vault_root)
        self.vault = self.source.vault
        capability = self.source.vault_manifest.get("source_units", {}).get("capabilities", {})
        if capability.get("vault_finalize") is not True:
            _fail("CAPABILITY_DISABLED", "Vault does not declare P4 Vault Finalize capability")

    def _state(self) -> dict[str, Any]:
        state = _load_json(_vault_path(self.vault, RELEASE_STATE_PATH))
        validate_record("knowledge_release_state", state)
        return state

    def _registry(self) -> dict[str, Any]:
        registry = _load_json(_vault_path(self.vault, IDENTITY_PATH))
        validate_record("identity_registry", registry)
        return registry

    def _plan_path(self, release_id: str) -> Path:
        return _vault_path(self.vault, f"{RELEASE_ROOT}/{release_id}/plan.json")

    def _manifest_path(self, release_id: str) -> Path:
        return _vault_path(self.vault, f"{RELEASE_ROOT}/{release_id}/manifest.json")

    def _page_revision(self, page_id: str, revision_id: str) -> dict[str, Any]:
        path = _vault_path(self.vault, f"{BUILD_ROOT}/page-revisions/{page_id}/{revision_id}.json")
        page = _load_json(path)
        validate_record("page_revision", page)
        if page["page_id"] != page_id or page["revision_id"] != revision_id or page["state"] != "committed":
            _fail("INVALID_STATE", "knowledge identity does not point to a committed page revision")
        return page

    def _current_sets(self) -> dict[str, dict[str, Any]]:
        root = _vault_path(self.vault, UNIT_ROOT)
        result: dict[str, dict[str, Any]] = {}
        for path in sorted(root.glob("*/current.json")) if root.exists() else []:
            value = _load_json(path)
            resource_id = str(value.get("resource_id", ""))
            if path.parent.name != resource_id or value.get("contract") != "hermes-source-unit-current/v1":
                _fail("INVALID_SCHEMA", f"invalid current UnitSet pointer: {path}")
            pointer = self.source._current(resource_id)
            self.source.validate(resource_id)
            result[resource_id] = pointer
        return result

    def _runs(self, run_ids: Iterable[str]) -> list[dict[str, Any]]:
        runs = []
        for run_id in _unique_strings(run_ids):
            self.build.validate_run(run_id)
            run = _load_json(_vault_path(self.vault, f"{BUILD_ROOT}/{run_id}/manifest.json"))
            if run.get("state") != "completed":
                _fail("INVALID_STATE", f"build is not completed: {run_id}")
            runs.append(run)
        return runs

    def _source_changes(self, raw: Iterable[Mapping[str, Any]],
                        current: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            value = dict(item)
            resource = str(value.get("resource_id", ""))
            if not resource or resource in seen:
                _fail("INVALID_SCHEMA", "source changes require unique resource_id", f"$.source_changes[{index}]")
            seen.add(resource)
            kind = value.get("change")
            previous = value.get("previous_unit_set_id")
            selected = current.get(resource)
            actual = selected.get("unit_set_id") if selected else None
            if kind == "replaced":
                if not isinstance(value.get("current_unit_set_id"), str) or value["current_unit_set_id"] != actual:
                    _fail("SOURCE_CHANGED", "replacement does not name the current UnitSet", f"$.source_changes[{index}]")
                if previous == actual:
                    _fail("INVALID_SCHEMA", "replacement must change the UnitSet", f"$.source_changes[{index}]")
            elif kind == "withdrawn":
                if value.get("current_unit_set_id") is not None:
                    _fail("INVALID_SCHEMA", "withdrawn source must have null current_unit_set_id", f"$.source_changes[{index}]")
            else:
                _fail("INVALID_SCHEMA", "source change must be replaced or withdrawn", f"$.source_changes[{index}]")
            changes.append(value)
        return sorted(changes, key=lambda item: item["resource_id"])

    @staticmethod
    def _contribution(ref: Mapping[str, Any], current: Mapping[str, Mapping[str, Any]],
                      changes: Mapping[str, Mapping[str, Any]]) -> str:
        unit = ref["unit_ref"]
        resource, unit_set = unit["resource_id"], unit["unit_set_id"]
        change = changes.get(resource)
        if change and change["change"] == "withdrawn" and unit_set == change["previous_unit_set_id"]:
            return "withdrawn"
        pointer = current.get(resource)
        return "active" if pointer and pointer["unit_set_id"] == unit_set else "stale"

    def _redirects(self, subjects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        redirects: list[dict[str, Any]] = []
        for subject in subjects:
            root = _vault_path(self.vault, f"{BUILD_ROOT}/page-revisions/{subject['page_id']}")
            prior_paths: dict[str, dict[str, Any]] = {}
            for sidecar in sorted(root.glob("*.json")) if root.exists() else []:
                revision = _load_json(sidecar)
                validate_record("page_revision", revision)
                if revision["state"] == "committed" and revision["path"] != subject["current_path"]:
                    prior_paths[revision["path"]] = revision
            for old_path, revision in sorted(prior_paths.items()):
                path = _vault_path(self.vault, old_path)
                if not path.exists():
                    continue
                content = path.read_bytes()
                marker = f"hermes_page_id: {subject['page_id']}\n".encode()
                if _sha(content) != revision["authored_sha256"] and marker not in content:
                    _fail("SOURCE_CHANGED", f"historical page path contains unrelated content: {old_path}")
                redirects.append({"from_path": old_path, "to_path": subject["current_path"],
                                  "page_id": subject["page_id"],
                                  "source_sha256": revision["authored_sha256"]})
        return redirects

    @staticmethod
    def _redirect_content(item: Mapping[str, Any]) -> bytes:
        target = str(item["to_path"])
        link = target[:-3] if target.endswith(".md") else target
        return ("---\nhermes_redirect: true\nhermes_page_id: " + str(item["page_id"]) +
                "\ntarget: " + target + "\n---\n\n# Redirect\n\n[[" + link + "]]\n").encode("utf-8")

    def _navigation(self, subjects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        lookup: dict[str, set[str]] = {}
        for subject in subjects:
            path = subject["current_path"]
            keys = [path, path[:-3] if path.endswith(".md") else path, Path(path).stem,
                    subject["canonical_name"], *subject["aliases"]]
            for key in keys:
                lookup.setdefault(str(key).casefold(), set()).add(subject["page_id"])
        outgoing: dict[str, set[str]] = {item["page_id"]: set() for item in subjects}
        blockers: list[str] = [f"ambiguous-alias:{key}:{','.join(sorted(values))}"
                               for key, values in sorted(lookup.items()) if len(values) > 1]
        for subject in subjects:
            path = _vault_path(self.vault, subject["current_path"])
            if not path.is_file():
                blockers.append(f"missing-page:{subject['page_id']}:{subject['current_path']}")
                continue
            for raw in _WIKILINK.findall(path.read_text(encoding="utf-8")):
                matches = lookup.get(raw.strip().casefold(), set())
                if len(matches) == 1:
                    target = next(iter(matches))
                    if target != subject["page_id"]:
                        outgoing[subject["page_id"]].add(target)
                elif len(matches) == 0:
                    blockers.append(f"dead-link:{subject['page_id']}:{raw.strip()}")
                else:
                    blockers.append(f"ambiguous-link:{subject['page_id']}:{raw.strip()}")
        incoming: dict[str, set[str]] = {item["page_id"]: set() for item in subjects}
        for source, targets in outgoing.items():
            for target in targets:
                incoming[target].add(source)
        entries = [{"page_id": item["page_id"], "subject_id": item["subject_id"],
                    "path": item["current_path"], "canonical_name": item["canonical_name"],
                    "aliases": item["aliases"], "directory": str(Path(item["current_path"]).parent).replace("\\", "/"),
                    "outgoing_page_ids": sorted(outgoing[item["page_id"]]),
                    "incoming_page_ids": sorted(incoming[item["page_id"]])}
                   for item in sorted(subjects, key=lambda row: row["page_id"])]
        return entries, _unique_strings(blockers)

    def _source_eligibility(self, current: Mapping[str, Mapping[str, Any]],
                            changes: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        governance = self.source.vault_manifest["governance"]
        registry = _load_json(_vault_path(self.vault, governance["repository"]["registry_path"]))
        organizations = _load_json(_vault_path(self.vault, governance["organizations_path"]))
        approved = {item.get("id") for item in organizations.get("organizations", [])
                    if isinstance(item, Mapping) and item.get("status") == "approved"}
        records = {item.get("resource_id"): item for item in registry.get("records", []) if isinstance(item, Mapping)}
        result = []
        for resource, pointer in sorted(current.items()):
            record = records.get(resource)
            reasons = []
            if changes.get(resource, {}).get("change") == "withdrawn":
                reasons.append("source-withdrawn")
            if not record or record.get("processing_status") != "completed":
                reasons.append("source-processing-incomplete")
            if not record or record.get("governance_status") != "active":
                reasons.append("source-governance-inactive")
            occurrences = record.get("source_occurrences", []) if record else []
            if not occurrences or any(item.get("source_organization_id") not in approved for item in occurrences):
                reasons.append("source-organization-unapproved")
            result.append({"kind": "source_unit_set", "id": pointer["unit_set_id"],
                           "eligible": not reasons, "reasons": reasons,
                           "resource_id": resource, "unit_set_id": pointer["unit_set_id"],
                           "page_revision_id": None, "source_unit_refs": []})
        for resource, change in sorted(changes.items()):
            if change["change"] == "withdrawn" and resource not in current:
                result.append({"kind": "source_unit_set", "id": change["previous_unit_set_id"],
                               "eligible": False, "reasons": ["source-withdrawn"],
                               "resource_id": resource,
                               "unit_set_id": change["previous_unit_set_id"],
                               "page_revision_id": None, "source_unit_refs": []})
        return result

    def _build_plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        release_id, actor = str(request["release_id"]), str(request["actor"])
        state = self._state()
        if int(request["expected_state_revision"]) != state["revision"]:
            _fail("REVISION_CONFLICT", "knowledge release state changed", "$.expected_state_revision")
        run_ids = list(request.get("build_run_ids", []))
        if not run_ids:
            run_ids = [path.parent.name for path in sorted(_vault_path(self.vault, BUILD_ROOT).glob("*/manifest.json"))
                       if path.parent.name not in state["applied_build_runs"]]
        runs = self._runs(run_ids)
        current = self._current_sets()
        changes = self._source_changes(request.get("source_changes", []), current)
        change_map = {item["resource_id"]: item for item in changes}
        registry = self._registry()
        active_subjects = [item for item in registry["subjects"] if item["status"] == "active"]
        included_pages = {page["page_id"] for run in runs for page in run["page_revisions"]}
        subject_rows, index_items = [], self._source_eligibility(current, change_map)
        for subject in active_subjects:
            page = self._page_revision(subject["page_id"], subject["current_revision_id"])
            path = _vault_path(self.vault, subject["current_path"])
            if not path.is_file() or _sha(path.read_bytes()) != page["authored_sha256"]:
                _fail("SOURCE_CHANGED", f"current page differs from committed revision: {subject['current_path']}")
            groups = {"active": [], "stale": [], "withdrawn": []}
            for ref in page["support_refs"]:
                groups[self._contribution(ref, current, change_map)].append(ref)
            affected = subject["page_id"] in included_pages or bool(groups["stale"] or groups["withdrawn"])
            disposition = "current"
            reasons: list[str] = []
            if groups["stale"] or groups["withdrawn"]:
                disposition = "review_required" if groups["active"] else "blocked"
                if groups["stale"]:
                    reasons.append("stale-source-contribution")
                if groups["withdrawn"]:
                    reasons.append("withdrawn-source-contribution")
                if not groups["active"]:
                    reasons.append("no-current-source-support")
            row = {"subject_id": subject["subject_id"], "page_id": subject["page_id"],
                   "current_revision_id": subject["current_revision_id"], "current_path": subject["current_path"],
                   "active_support_refs": groups["active"], "stale_support_refs": groups["stale"],
                   "withdrawn_support_refs": groups["withdrawn"], "disposition": disposition,
                   "reasons": reasons, "affected": affected}
            subject_rows.append(row)
            eligibility_reasons = list(reasons)
            if page["qa_status"] != "usable":
                eligibility_reasons.append("qa-required")
            if page["business_status"] != "approved":
                eligibility_reasons.append("business-unassessed")
            if page["visibility"] != "published":
                eligibility_reasons.append("visibility-draft")
            index_items.append({"kind": "knowledge_page", "id": subject["page_id"],
                                "eligible": not eligibility_reasons, "reasons": _unique_strings(eligibility_reasons),
                                "resource_id": None, "unit_set_id": None,
                                "page_revision_id": subject["current_revision_id"],
                                "source_unit_refs": groups["active"]})
        navigation, blockers = self._navigation(active_subjects)
        redirects = self._redirects(active_subjects)
        if not runs and not changes and not str(request.get("reason", "")).strip():
            _fail("INVALID_STATE", "an empty release requires a reason")
        base = {"contract": "hermes-vault-finalize-plan/v1", "release_id": release_id,
                "revision": 1, "state": "draft", "actor": actor,
                "expected_state_revision": state["revision"],
                "identity_registry_revision": registry["revision"],
                "build_runs": [{"run_id": run["run_id"], "revision": run["revision"]} for run in runs],
                "source_changes": changes, "subjects": subject_rows, "redirects": redirects,
                "navigation": navigation, "index_eligibility": index_items,
                "blockers": blockers, "reason": str(request.get("reason", ""))}
        base["plan_id"] = fingerprint({key: value for key, value in base.items()
                                        if key not in ("revision", "state")})
        validate_record("vault_finalize_plan", base)
        return base

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        plan = self._build_plan(request)
        path = self._plan_path(plan["release_id"])
        if path.exists():
            existing = _load_json(path)
            if existing != plan:
                _fail("DUPLICATE", "release_id already identifies a different plan")
            return {"ok": True, "created": False, "plan": existing}
        _write_atomic(path, _json_bytes(plan))
        return {"ok": True, "created": True, "plan": plan}

    def apply(self, request: Mapping[str, Any]) -> dict[str, Any]:
        lock = _vault_path(self.vault, f"{RELEASE_ROOT}/.finalize.lock")
        with _exclusive_lock(lock):
            return self._apply_locked(request)

    def _apply_locked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        release_id, actor = str(request["release_id"]), str(request["actor"])
        manifest_path = self._manifest_path(release_id)
        if manifest_path.exists():
            manifest = _load_json(manifest_path)
            self.validate(release_id)
            return {"ok": True, "committed": False, "release": manifest}
        plan = _load_json(self._plan_path(release_id))
        validate_record("vault_finalize_plan", plan)
        if plan["actor"] != actor or plan["plan_id"] != request["plan_id"]:
            _fail("ACCESS_DENIED", "apply must use the owning actor and exact plan")
        if int(request["expected_revision"]) not in (plan["revision"], plan["revision"] - 1):
            _fail("REVISION_CONFLICT", "Finalize plan changed")
        if plan["blockers"]:
            _fail("INVALID_STATE", "Finalize plan has unresolved blockers", "$.blockers")
        state = self._state()
        recovering = (state["revision"] == plan["expected_state_revision"] + 1
                      and state["current_release_id"] == release_id
                      and all(item["run_id"] in state["applied_build_runs"] for item in plan["build_runs"]))
        if state["revision"] == plan["expected_state_revision"]:
            if plan["state"] != "draft":
                _fail("INVALID_STATE", "applied plan has no matching release state")
            rebuilt = self._build_plan({"release_id": release_id, "actor": actor,
                                        "expected_state_revision": plan["expected_state_revision"],
                                        "build_run_ids": [item["run_id"] for item in plan["build_runs"]],
                                        "source_changes": plan["source_changes"], "reason": plan["reason"]})
            if rebuilt != plan:
                _fail("SOURCE_CHANGED", "Vault changed after Finalize plan")
        elif not recovering:
            _fail("REVISION_CONFLICT", "knowledge release state changed during apply")
        for item in plan["redirects"]:
            path = _vault_path(self.vault, item["from_path"])
            actual = _sha(path.read_bytes())
            if actual not in (item["source_sha256"], _sha(self._redirect_content(item))):
                _fail("SOURCE_CHANGED", f"redirect source changed: {item['from_path']}")
        navigation = {"contract": "hermes-knowledge-navigation/v1", "release_id": release_id,
                      "entries": plan["navigation"], "redirects": plan["redirects"]}
        validate_record("knowledge_navigation", navigation)
        navigation_bytes = _json_bytes(navigation)
        for item in plan["redirects"]:
            _write_atomic(_vault_path(self.vault, item["from_path"]), self._redirect_content(item))
        versioned_navigation = _vault_path(self.vault, f"{RELEASE_ROOT}/{release_id}/navigation.json")
        _write_atomic(versioned_navigation, navigation_bytes)
        _write_atomic(_vault_path(self.vault, NAVIGATION_PATH), navigation_bytes)
        new_state = copy.deepcopy(state)
        if not recovering:
            new_state["revision"] += 1
            new_state["current_release_id"] = release_id
            new_state["applied_build_runs"] = _unique_strings(
                [*state["applied_build_runs"], *[item["run_id"] for item in plan["build_runs"]]])
            new_state["releases"] = _unique_strings([*state["releases"], release_id])
        validate_record("knowledge_release_state", new_state)
        applied_plan = (plan if plan["state"] == "applied"
                        else {**plan, "state": "applied", "revision": plan["revision"] + 1})
        release = {"contract": "hermes-knowledge-release/v1", "release_id": release_id,
                   "plan_id": plan["plan_id"], "actor": actor, "state": "completed",
                   "state_revision": new_state["revision"],
                   "identity_registry_revision": plan["identity_registry_revision"],
                   "build_runs": plan["build_runs"], "source_changes": plan["source_changes"],
                   "subjects": plan["subjects"], "redirects": plan["redirects"],
                   "navigation_sha256": _sha(navigation_bytes),
                   "index_eligibility": plan["index_eligibility"],
                   "blockers": plan["blockers"], "reason": plan["reason"]}
        validate_record("knowledge_release", release)
        _write_atomic(_vault_path(self.vault, RELEASE_STATE_PATH), _json_bytes(new_state))
        _write_atomic(self._plan_path(release_id), _json_bytes(applied_plan))
        _write_atomic(manifest_path, _json_bytes(release))
        return {"ok": True, "committed": True, "release": release}

    def validate(self, release_id: str) -> dict[str, Any]:
        release = _load_json(self._manifest_path(release_id))
        validate_record("knowledge_release", release)
        plan = _load_json(self._plan_path(release_id))
        validate_record("vault_finalize_plan", plan)
        if plan["state"] != "applied" or plan["plan_id"] != release["plan_id"]:
            _fail("INVALID_STATE", "release plan is not the applied plan")
        navigation_path = _vault_path(self.vault, f"{RELEASE_ROOT}/{release_id}/navigation.json")
        navigation = _load_json(navigation_path)
        validate_record("knowledge_navigation", navigation)
        if _sha(_json_bytes(navigation)) != release["navigation_sha256"]:
            _fail("SOURCE_CHANGED", "versioned navigation projection changed")
        for run in release["build_runs"]:
            actual = _load_json(_vault_path(self.vault, f"{BUILD_ROOT}/{run['run_id']}/manifest.json"))
            if actual.get("revision") != run["revision"] or actual.get("state") != "completed":
                _fail("SOURCE_CHANGED", f"pinned build changed: {run['run_id']}")
        state = self._state()
        if release_id not in state["releases"]:
            _fail("UNRESOLVED_REFERENCE", "release is absent from release state")
        if state["current_release_id"] == release_id:
            current_navigation = _vault_path(self.vault, NAVIGATION_PATH)
            if _sha(current_navigation.read_bytes()) != release["navigation_sha256"]:
                _fail("SOURCE_CHANGED", "current navigation differs from current release")
            for item in release["redirects"]:
                path = _vault_path(self.vault, item["from_path"])
                if path.read_bytes() != self._redirect_content(item):
                    _fail("SOURCE_CHANGED", f"current redirect changed: {item['from_path']}")
        return {"ok": True, "release_id": release_id, "state": release["state"],
                "subject_count": len(release["subjects"]),
                "eligible_count": sum(item["eligible"] for item in release["index_eligibility"]),
                "blocked_count": sum(item["disposition"] == "blocked" for item in release["subjects"])}

    def status(self) -> dict[str, Any]:
        return self._state()
