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

from hermes_source_units import ContractError, FileIngestWorkflowService, mutation_digest
from hermes_source_units.ingest_workflow import WORKER_KINDS
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units.source_units import _exclusive_lock, _vault_path
from hermes_source_units.runtime_telemetry import span

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


def dispatch_ready_programs(vault, workflow_id):
    """Use the existing native CLI; no permission or graph snapshot handoff."""
    with span('dispatch_cli', vault=vault, workflow_id=workflow_id):
        result = subprocess.run(['hermes', 'ingest-program-dispatch', '--vault', str(vault),
            '--workflow-id', workflow_id], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'native program dispatch failed')
    return {'state':'dispatched'}


def dispatch(vault: str | Path, command: str, request: Mapping[str, Any], *, dry_run=False) -> dict[str, Any]:
    adapter = IngestKanbanAdapter(vault, enable_workers=configuration()["worker_dispatch_enabled"])
    if command == "start":
        value = start_and_pin(vault, request)
        result = adapter.sync(_mutation(value))
    elif command == 'amend-execution':
        result = adapter.amend_execution(request, dry_run=dry_run)
    elif command == 'recover-pass-execution':
        result = adapter.recover_pass_execution(request, dry_run=dry_run)
    elif command == "worker-prepare-source":
        from source_preparation import prepare_source
        result = prepare_source(adapter, request)
    elif command == "worker-plan-exact":
        from exact_preparation import prepare_exact
        result = prepare_exact(adapter, request)
    else:
        result = getattr(adapter, command.replace("-", "_"))(request)
    if (command in ("start", "resume", "continue-workflow", "revise-passes") and result.get("background_dispatch")
            and os.name == "posix"):
        if command == 'resume' and result.get('program_dispatch'):
            try:
                lock = _vault_path(adapter.workflow.vault,
                    f'_system/ledgers/ingest-workflows/.watch-locks/{result["workflow_id"]}.lock')
                # An existing watcher already owns dispatch. Do not race its
                # separate native capacity snapshot by claiming other cards.
                with _exclusive_lock(lock), span('resume_program_handoff', vault=vault, workflow_id=result['workflow_id']):
                    result['initial_program_dispatch'] = dispatch_ready_programs(vault, result['workflow_id'])
            except (ContractError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                result['initial_program_dispatch'] = {'state':'deferred','error':str(exc),
                    'partial_spawn_possible':True}
        script = SKILL / "scripts/watch_ingest_workflow.py"
        name = hashlib.sha256(str(result["workflow_id"]).encode()).hexdigest()[:16]
        log = Path(tempfile.gettempdir()) / f"hermes-ingest-watch-{name}.log"
        with log.open("ab") as output:
            with span('watcher_launch', vault=vault, workflow_id=result['workflow_id']):
                process = subprocess.Popen(
                    [sys.executable, str(script), "--vault", str(vault),
                     "--workflow-id", str(result["workflow_id"])],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True)
        result["reconciler_pid"] = process.pid
        result["reconciler_log"] = str(log)
    return result
