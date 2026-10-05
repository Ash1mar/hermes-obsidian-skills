"""Durable operator continuation gates, independent of dispatch recovery."""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Mapping

from .source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from .source_units import UNIT_ROOT
from .validation import ContractError, fingerprint, validate_record

PAUSE_BOUNDARIES = ("exact_plan", "canary", "pass", "checkpoint_1",
                    "build_finalize", "checkpoint_2", "release_sync")


def _fail(code, message):
    raise ContractError(code, "$", message)


class WorkflowPauseMixin:
    def _task_passes_complete(self, task_id):
        root = self.vault / f"_system/knowledge-builds/task-{task_id}/passes"
        records = [_load_json(p) for p in sorted(root.glob("*.json"))]
        for record in records:
            validate_record("knowledge_pass", record)
            if record["task_id"] != task_id:
                _fail("STALE_INPUT", "Pass record names another task")
        sequences = {p["sequence"] for p in records}
        return (len(sequences) >= 2 and sequences == set(range(max(sequences) + 1))
                and any(p["sequence"] == 0 and p["pass_kind"] == "candidate" for p in records)
                and any(p["sequence"] > 0 and p["pass_kind"] == "citation" for p in records))

    def pending_pause(self, value: Mapping[str, Any]) -> str | None:
        """Also detect an unlatched boundary, so worker writes fail closed."""
        control = value.get("pause_control", {})
        if control.get("boundary"):
            return control["boundary"]
        wanted = value["scope"].get("pause_after", [])
        released = {item["boundary"] for item in control.get("history", [])}
        if not wanted or not value["batch_id"] or value["cancel_requested"]:
            return None
        stage = value["current_stage"]
        policy = value["dispatch_policy"]
        slices = None
        for boundary in PAUSE_BOUNDARIES:
            if boundary not in wanted or boundary in released:
                continue
            ready = False
            if boundary == "exact_plan":
                ready = stage == "analyzing"
            elif boundary in ("canary", "pass") and stage == "analyzing":
                slices = slices if slices is not None else self.knowledge._slices(value["batch_id"])
                selected = (set(policy["slice_ids"]) if boundary == "canary"
                            else {s["slice_id"] for s in slices})
                records = [s for s in slices if s["slice_id"] in selected]
                ready = (bool(selected) and len(records) == len(selected)
                         and (boundary != "canary" or len(selected) == 8)
                         and all(s["state"] == "completed" and s["result_refs"] for s in records))
                if boundary == "pass" and not slices:
                    tasks = self.knowledge._batch(value["batch_id"])["task_ids"]
                    ready = bool(tasks) and all(self._task_passes_complete(t) for t in tasks)
            elif boundary in ("checkpoint_1", "checkpoint_2"):
                ready = stage == boundary and value["checkpoints"][boundary]["state"] == "approved"
            elif boundary == "build_finalize" and stage == "build_finalizing":
                batch = self.knowledge._batch(value["batch_id"])
                ready = batch["state"] == "completed" and bool(batch["run_ids"])
            elif boundary == "release_sync":
                ready = (stage == "validating" or (stage == "indexing"
                         and value["scope"].get("provider", "skip") == "skip"))
            if ready:
                return boundary
        return None

    def assert_not_paused(self, value: Mapping[str, Any]) -> None:
        boundary = self.pending_pause(value)
        if boundary:
            _fail("WORKFLOW_PAUSED", f"operator continuation required after {boundary}")

    def pause_status(self, value: Mapping[str, Any]) -> dict[str, Any]:
        control = value.get("pause_control", {})
        return {"pause_after": value["scope"].get("pause_after", []),
                "boundary": self.pending_pause(value),
                "evidence_digest": control.get("evidence_digest"),
                "report_ref": control.get("report_ref"),
                "continued": [item["boundary"] for item in control.get("history", [])]}

    def _pause_evidence(self, value, boundary):
        """Pin authoritative bytes, never volatile Kanban status or ledger revision."""
        self.pinned_templates(value)
        files = {}
        trees = set()
        validated_sets = set()
        def pin(path):
            relative = path if isinstance(path, str) else path.relative_to(self.vault).as_posix()
            path = _vault_path(self.vault, relative)
            files[path.relative_to(self.vault).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        def tree(path):
            if path in trees:
                return
            trees.add(path)
            if path.is_dir():
                for p in sorted(path.rglob("*")):
                    if p.is_file() and not any(part.startswith(".") for part in p.relative_to(path).parts):
                        pin(p)
        tree(self.vault / "_system/metadata")
        pin(self.vault / "_system/vault.json")
        for outcome in value.get("source_outcomes", []):
            pin(outcome["path"])
            if files[outcome["path"]] != outcome["content_sha256"]:
                _fail("SOURCE_CHANGED", "source bytes changed after preparation")
            for ref in outcome["artifact_refs"]:
                pin(ref)
            if outcome["status"] == "ready":
                source = self.knowledge.source
                current = source._current(outcome["resource_id"])
                if not current or current["unit_set_id"] != outcome["unit_set_id"]:
                    _fail("STALE_INPUT", "prepared UnitSet is no longer current")
                source.validate(outcome["resource_id"], outcome["unit_set_id"])
                validated_sets.add((outcome["resource_id"], outcome["unit_set_id"]))
                root = self.vault / UNIT_ROOT / outcome["resource_id"]
                tree(root)
        batch = self.knowledge._batch(value["batch_id"])
        if (batch["actor"] != value["actor"] or batch.get("cancel_requested")
                or "sha256:" + fingerprint(batch["task_ids"]) != value["pinned_inputs"]["task_ids_fingerprint"]):
            _fail("STALE_INPUT", "batch identity or task coverage changed")
        pin(self.knowledge._batch_path(value["batch_id"]))
        tree(self.knowledge._batch_path(value["batch_id"]).with_suffix(""))
        for task_id in batch["task_ids"]:
            task = self.knowledge._task(task_id)
            pin(self.knowledge._task_path(task_id))
            tree(self.vault / f"_system/knowledge-builds/task-{task_id}")
            for ref in task["target_refs"]:
                unit = ref["unit_ref"]
                source = self.knowledge.source
                identity = (unit["resource_id"], unit["unit_set_id"])
                if identity not in validated_sets:
                    source.validate(*identity)
                    validated_sets.add(identity)
                tree(self.vault / UNIT_ROOT / unit["resource_id"])
        for run_id in batch["run_ids"]:
            self.knowledge.validate_run(run_id)
            tree(self.knowledge._run_path(run_id).parent)
        for artifact in value["artifacts"]:
            for ref in artifact["refs"]:
                pin(ref)
        for pinning in value["template_pins"]:
            pin(pinning["path"])
        for revision in [*value.get('pass_revision_history', []), *([value['pass_revision']] if value.get('pass_revision') else [])]:
            self.revision_instructions({**value, 'pass_revision': revision})
            for ref in (revision['request_ref'], revision['before_ref'], revision['template_ref'],
                        *[item['ref'] for item in revision['evidence_hashes']]):
                pin(ref)
        if boundary in ("canary", "pass"):
            chosen = (value["dispatch_policy"]["slice_ids"] if boundary == "canary"
                      else [s["slice_id"] for s in self.knowledge._slices(value["batch_id"])])
            for sid in chosen:
                item = self.knowledge._slice(value["batch_id"], sid)
                if item["state"] != "completed" or not item["result_refs"]:
                    _fail("INCOMPLETE_COVERAGE", "pause requires durable completed Pass slices")
                for ref in item["result_refs"]:
                    validate_record("knowledge_pass", _load_json(_vault_path(self.vault, ref)))
            if boundary == "pass" and not all(self._task_passes_complete(t) for t in batch["task_ids"]):
                _fail("INCOMPLETE_COVERAGE", "every planned task requires candidate and citation Passes")
        for name, checkpoint in value["checkpoints"].items():
            if checkpoint.get("decision_ref"):
                pin(checkpoint["decision_ref"])
                if files[checkpoint["decision_ref"]] != checkpoint["decision_sha256"]:
                    _fail("SOURCE_CHANGED", "checkpoint decision evidence changed")
        if value.get("release_id"):
            tree(self.vault / "_system/knowledge-releases" / value["release_id"])
        if boundary == "release_sync" and value["scope"].get("provider") == "sync":
            pin("_system/reports/retrieval-index-manifest.json")
        return {"workflow_id": value["workflow_id"], "boundary": boundary,
                "stage": value["current_stage"], "scope": value["scope"],
                "pinned_inputs": value["pinned_inputs"], "dispatch_policy": value["dispatch_policy"],
                "source_outcomes": value.get("source_outcomes", []),
                "checkpoints": value["checkpoints"], "files": files}

    def latch_pause(self, request):
        """Trusted reconciler records the first reached, unacknowledged boundary."""
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._mutation(request)
            boundary = self.pending_pause(value)
            if not boundary or value.get("pause_control", {}).get("boundary"):
                return value
            if boundary == "exact_plan" and not self.knowledge._batch(value["batch_id"]).get("slices_initialized"):
                return value  # Next sync initializes slices before snapshotting their evidence.
            evidence = self._pause_evidence(value, boundary)
            digest = "sha256:" + fingerprint(evidence)
            ref = f"_system/ledgers/ingest-workflows/{value['workflow_id']}/pauses/{boundary}.json"
            report = {"contract": "hermes-ingest-pause/v1", "evidence_digest": digest, "evidence": evidence}
            path = _vault_path(self.vault, ref)
            if path.exists() and _load_json(path) != report:
                _fail("STALE_INPUT", "pause report already differs")
            _write_atomic(path, _json_bytes(report))
            value["pause_control"] = {"boundary": boundary, "evidence_digest": digest,
                "report_ref": ref, "history": value.get("pause_control", {}).get("history", [])}
            value["revision"] += 1
            self._write(value)
            return value

    def continue_workflow(self, request):
        """Explicitly authorize exactly one boundary; resume never calls this."""
        self._check_request(request)
        if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
            _fail("ACCESS_DENIED", "workers cannot authorize continuation")
        continue_id = request.get("continue_id", "")
        if not isinstance(continue_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", continue_id):
            _fail("INVALID_SCHEMA", "continuation requires a stable continue_id")
        with _exclusive_lock(self._lock(str(request["workflow_id"]))):
            value = self._load(str(request["workflow_id"]))
            if value["actor"] != request["actor"]:
                _fail("ACTOR_MISMATCH", "workflow actor differs")
            control = value.get("pause_control", {})
            for old in control.get("history", []):
                if old["continue_id"] == continue_id:
                    if old["input_digest"] != request["input_digest"]:
                        _fail("IDEMPOTENCY_CONFLICT", "continuation ID was already used")
                    if _load_json(_vault_path(self.vault, old["report_ref"])) != dict(request):
                        _fail("SOURCE_CHANGED", "continuation audit record changed")
                    return value  # Does not release a later boundary.
            value = self._mutation(request)
            control = value.get("pause_control", {})
            if value.get('pass_revision') and value['pass_revision']['state'] != 'accepted':
                _fail('REVISION_REVIEW_REQUIRED', 'semantic revision must be reviewed before continuation')
            if value["cancel_requested"] or value["state"] in ("completed", "partial", "failed"):
                _fail("WORKFLOW_STOPPED", "stopped workflow cannot continue")
            boundary = control.get("boundary")
            if not boundary or request.get("boundary") != boundary:
                _fail("INVALID_TRANSITION", "request must name the current latched pause")
            report = _load_json(_vault_path(self.vault, control["report_ref"]))
            evidence = self._pause_evidence(value, boundary)
            digest = "sha256:" + fingerprint(evidence)
            if (request.get("evidence_digest") != control["evidence_digest"]
                    or digest != control["evidence_digest"] or report.get("evidence") != evidence
                    or report.get("evidence_digest") != digest):
                _fail("STALE_INPUT", "pause evidence changed; inspect before continuing")
            ref = f"_system/ledgers/ingest-workflows/{value['workflow_id']}/continuations/{continue_id}.json"
            audit_path = _vault_path(self.vault, ref)
            if audit_path.exists() and _load_json(audit_path) != dict(request):
                _fail("IDEMPOTENCY_CONFLICT", "continuation audit ID already differs")
            _write_atomic(audit_path, _json_bytes(dict(request)))
            control["history"].append({"boundary": boundary, "continue_id": continue_id,
                "actor": value["actor"], "evidence_digest": digest,
                "input_digest": request["input_digest"], "report_ref": ref})
            control.update(boundary=None, evidence_digest=None, report_ref=None)
            value["revision"] += 1
            self._write(value)
            return value
