"""Serialize worker domain commits with workflow cancellation and migration."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path

from .validation import ContractError, fingerprint

_ACTIVE = ContextVar("ingest_write_guard", default=None)


def _fail(code, message):
    raise ContractError(code, "$", message)


@contextmanager
def workflow_write_guard(vault, *, kinds=("source-prepare",), actor=None,
                         workflow_id=None, task_id=None):
    # Imports are local because the workflow service itself uses SourceUnits.
    from .ingest_workflow import FileIngestWorkflowService, TERMINAL, WORKFLOW_ROOT
    from .source_units import _exclusive_lock, _load_json, _vault_path

    vault = Path(vault).resolve()
    inherited = _ACTIVE.get()
    if inherited is not None:
        if (inherited["vault"] != str(vault) or inherited["kind"] not in kinds
                or (actor is not None and actor != inherited["actor"])):
            _fail("ACCESS_DENIED", "nested domain write exceeds worker scope")
        yield inherited
        return
    env_task = os.environ.get("HERMES_KANBAN_TASK")
    board = os.environ.get("HERMES_KANBAN_BOARD", "")
    if env_task and task_id and env_task != task_id:
        _fail("ACCESS_DENIED", "worker cannot use another task binding")
    task_id = task_id or env_task
    if not workflow_id and not task_id:
        yield None  # Existing foreground domain API remains supported.
        return
    service = FileIngestWorkflowService(vault)
    if not workflow_id:
        matches = []
        for path in sorted((vault / WORKFLOW_ROOT).glob("*.json")):
            value = _load_json(path)
            if (value["kanban"]["board_id"] == board or any(
                    item.get("task_id") == task_id for item in value["kanban"]["task_map"])):
                matches.append(value["workflow_id"])
        if len(matches) != 1:
            _fail("STALE_INPUT", "worker has no unique workflow binding in this Vault")
        workflow_id = matches[0]
    with _exclusive_lock(service._lock(workflow_id)):
        value = service.status(workflow_id)
        if value["cancel_requested"] or value["state"] in TERMINAL:
            _fail("WORKFLOW_STOPPED", "domain write rejected: workflow is stopped")
        if actor is not None and actor != value["actor"]:
            _fail("ACTOR_MISMATCH", "domain writer differs from workflow actor")
        cards = [item for item in value["kanban"]["task_map"] if item.get("task_id") == task_id]
        if len(cards) != 1 or (board and board != value["kanban"]["board_id"]):
            _fail("STALE_INPUT", "worker card has been superseded")
        kind = cards[0]["node"].partition(":")[0]
        if kind not in kinds:
            _fail("ACCESS_DENIED", "worker kind cannot perform this domain write")
        service.pinned_templates(value)
        if kind == "source-prepare" and (value["batch_id"] is not None or
                value["current_stage"] not in ("created", "source_preparing")):
            _fail("WORKFLOW_STOPPED", "source write is outside source preparation")
        source = None
        for path in value["scope"]["source_paths"] if kind == "source-prepare" else []:
            sha = hashlib.sha256(_vault_path(vault, path).read_bytes()).hexdigest()
            if cards[0]["node"] == "source-prepare:" + fingerprint({
                    "path": path, "content_sha256": sha})[:16]:
                source = {"path": path, "content_sha256": sha}
                break
        if kind == "source-prepare" and (source is None or any(
                item["path"] == source["path"] for item in value.get("source_outcomes", []))):
            _fail("STALE_INPUT", "source changed or already has an outcome")
        context = {"vault": str(vault), "kind": kind, "source": source,
                   "workflow_id": workflow_id, "actor": value["actor"]}
        token = _ACTIVE.set(context)
        try:
            yield context
        finally:
            _ACTIVE.reset(token)


def require_source(context, source_sha):
    if context and context.get("source") and context["source"]["content_sha256"] != source_sha:
        _fail("ACCESS_DENIED", "domain output belongs to a different source")
