#!/usr/bin/env python3
"""Reconcile worker Vault outcomes from a trusted, non-worker process."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import subprocess
from pathlib import Path
import sys
import time
import signal
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _vault_path
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units.runtime_telemetry import runtime_directory, span
from hermes_source_units.source_units import _json_bytes, _write_atomic
from orchestration import dispatch_ready_programs


@contextmanager
def wake_events(vault, workflow_id):
    event = threading.Event()
    marker = runtime_directory(vault,workflow_id)/'watcher.json'
    previous = signal.signal(signal.SIGUSR1,lambda *_:event.set()) if os.name=='posix' else None
    try:
        try:
            _write_atomic(marker,_json_bytes({'pid':os.getpid(),'workflow_id':workflow_id}))
        except OSError:
            pass  # Periodic recovery remains available without the wake hint.
        yield event
    finally:
        if os.name=='posix':
            signal.signal(signal.SIGUSR1,previous)
        try:
            if json.loads(marker.read_text())['pid']==os.getpid():
                marker.unlink()
        except (OSError,ValueError,KeyError):
            pass


def watch(vault: str, workflow_id: str, interval: float = 3) -> None:
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") or os.environ.get("HERMES_KANBAN_TASK"):
        raise RuntimeError("reconciler must run outside a Hermes worker or delegated child")
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    lock = _vault_path(adapter.workflow.vault,
        f"_system/ledgers/ingest-workflows/.watch-locks/{workflow_id}.lock")
    with _exclusive_lock(lock), wake_events(vault,workflow_id) as wake:
        delay = interval
        prior = None
        while True:
            # Consume the prior hint before reading fresh state. A hint arriving
            # during this cycle remains set and prevents the following sleep.
            wake.clear()
            value = adapter.workflow.status(workflow_id)
            if value["cancel_requested"] or value["state"] in ("completed", "partial", "failed"):
                return
            try:
                with span('watch_cycle',vault=vault,workflow_id=workflow_id):
                    # Existing ready bindings can refill a free slot without
                    # waiting for the full graph projection. Native CLI rereads
                    # authority, settles exact ended grants, then admits claims.
                    try:
                        with span('refill_handoff',vault=vault,workflow_id=workflow_id):
                            dispatch_ready_programs(vault,workflow_id)
                    except (ContractError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        print('Ready-card handoff deferred: ' + str(exc), flush=True)
                        if getattr(exc,'code',None)=='LOCK_BUSY' or 'LOCK_BUSY' in str(exc):
                            # One busy atomic commit must not trigger a long full
                            # scan. Return to fresh completion/refill after a tick.
                            wake.wait(interval)
                            continue
                    if wake.is_set():
                        continue
                    result = adapter._sync_current(workflow_id,max_attempts=1,yield_requested=wake.is_set)
                if (result.get("pause", {}).get("boundary")
                        and result["pause"].get("evidence_digest") and not result.get('background_dispatch')):
                    print("Workflow paused after " + result["pause"]["boundary"], flush=True)
                    return
                if result.get('execution_blocked') and not result.get('background_dispatch'):
                    print('Execution blocked; trusted repair required: ' + ', '.join(result['execution_blocked']), flush=True)
                    return
                if result.get("canary_complete") and value["scope"].get("execution_mode") == "canary_only":
                    return
                if result.get('program_dispatch') and result.get('background_dispatch'):
                    # Hermes CLI loads its native runtime and backend plugin even
                    # when the operator launched this watcher with system python3.
                    dispatch_ready_programs(vault,workflow_id)
                stamp = (result.get('revision'),result.get('state'),result.get('program_dispatch'))
                delay = min(delay*2,max(interval,12)) if stamp==prior else interval
                prior = stamp
            except ContractError as exc:
                if exc.code not in ("KANBAN_UNAVAILABLE", "REVISION_CONFLICT", "LOCK_BUSY", "RECONCILIATION_YIELD"):
                    raise
                delay = 0 if exc.code=='RECONCILIATION_YIELD' else interval
            with span('reconciliation_wait',vault=vault,workflow_id=workflow_id,requested_seconds=delay):
                wake.wait(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    try:
        watch(args.vault, args.workflow_id, args.interval)
    except (ContractError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
