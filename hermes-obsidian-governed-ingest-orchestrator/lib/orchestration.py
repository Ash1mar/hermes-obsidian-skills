"""Small deterministic entry point around the Vault workflow and pinned templates."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from hermes_source_units import FileIngestWorkflowService, mutation_digest
from hermes_source_units.ingest_workflow import WORKER_KINDS
from ingest_kanban import IngestKanbanAdapter

SKILL = Path(__file__).resolve().parents[1]


def configuration() -> dict[str, Any]:
    return json.loads((SKILL / "config/orchestration.json").read_text(encoding="utf-8"))


def worker_pack() -> list[dict[str, str]]:
    version = configuration()["template_version"]
    return [{"kind": kind, "template_id": f"governed-ingest/{version}/{kind}",
             "content": (SKILL / "references/workers" / f"{kind}.md").read_text(encoding="utf-8")}
            for kind in WORKER_KINDS]


def _mutation(value: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
    request = {"workflow_id": value["workflow_id"], "actor": value["actor"],
               "expected_revision": value["revision"], **fields}
    request["input_digest"] = mutation_digest(request)
    return request


def start_and_pin(vault: str | Path, request: Mapping[str, Any]) -> dict[str, Any]:
    service = FileIngestWorkflowService(vault)
    value = service.start(request)
    if not value.get("template_pins"):
        value = service.pin_templates(_mutation(value, templates=worker_pack()))
    service.pinned_templates(value)
    return value


def dispatch(vault: str | Path, command: str, request: Mapping[str, Any]) -> dict[str, Any]:
    adapter = IngestKanbanAdapter(vault, enable_workers=configuration()["worker_dispatch_enabled"])
    if command == "start":
        value = start_and_pin(vault, request)
        result = adapter.sync(_mutation(value))
    else:
        result = getattr(adapter, command.replace("-", "_"))(request)
    if (command in ("start", "resume") and result.get("background_dispatch")
            and os.name == "posix"):
        script = SKILL / "scripts/watch_ingest_workflow.py"
        name = hashlib.sha256(str(result["workflow_id"]).encode()).hexdigest()[:16]
        log = Path(tempfile.gettempdir()) / f"hermes-ingest-watch-{name}.log"
        with log.open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, str(script), "--vault", str(vault),
                 "--workflow-id", str(result["workflow_id"])],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
        result["reconciler_pid"] = process.pid
        result["reconciler_log"] = str(log)
    return result
