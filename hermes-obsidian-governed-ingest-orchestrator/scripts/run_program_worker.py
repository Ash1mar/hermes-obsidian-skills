#!/usr/bin/env python3
"""Run a deterministic node owned by a live Hermes native Kanban run."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes, _write_atomic
from ingest_kanban import IngestKanbanAdapter
from program_worker import canonical_binding, execute


def run(vault, binding):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc, kanban_db_dispatch as kd
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    request, value = canonical_binding(adapter, binding)
    if (os.environ.get('HERMES_INGEST_EXECUTOR') != 'program-v1'
            or os.environ.get('HERMES_KANBAN_TASK') != request['task_id']
            or os.environ.get('HERMES_KANBAN_BOARD') != value['kanban']['board_id']):
        raise RuntimeError('program execution requires its native dispatcher grant')
    run_id = int(os.environ['HERMES_KANBAN_RUN_ID'])
    board = value['kanban']['board_id']
    def own_run():
        with contextlib.closing(kc.connect(board=board)) as conn:
            task = kb.get_task(conn, request['task_id'])
            return (task is not None and task.status == 'running' and task.current_run_id == run_id
                and task.claim_lock == os.environ.get('HERMES_KANBAN_CLAIM_LOCK') and bool(task.worker_pid))
    # Do not write domain state before the parent persists our process identity.
    deadline = time.monotonic() + 10
    while not own_run():
        if time.monotonic() >= deadline:
            raise RuntimeError('native run registration missing or superseded')
        time.sleep(.05)
    stop = threading.Event()
    completed = threading.Event()
    def heartbeat():
        while not stop.wait(30):
            try:
                with contextlib.closing(kc.connect(board=board)) as conn:
                    alive = own_run() and kd.heartbeat_worker(conn, request['task_id'], expected_run_id=run_id,
                        note='program executor; no model session')
            except Exception:
                alive = False
            if not alive:
                if not completed.is_set():
                    os.kill(os.getpid(), signal.SIGTERM)
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    progress = Path(binding).with_suffix('.execution.json')
    try:
        _write_atomic(progress, _json_bytes({'state':'running', 'executor':'program-v1', 'run_id':run_id}))
        result = execute(adapter, binding)
        completed.set()
        _write_atomic(progress, _json_bytes({'state':'completed' if result.get('ok') else 'blocked',
            'executor':'program-v1', 'run_id':run_id, 'result':result}))
        # The trusted reconciler acknowledges the durable result and holds
        # successors at pauses. Keep the native PID alive until it does so.
        deadline = time.monotonic() + 120
        while own_run() and time.monotonic() < deadline:
            time.sleep(.5)
        return result
    except (ContractError, OSError, ValueError, KeyError, RuntimeError) as exc:
        completed.set()
        ref = adapter.execution_failure(request, getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED'), str(exc))
        _write_atomic(progress, _json_bytes({'state':'execution_blocked', 'executor':'program-v1',
            'run_id':run_id, 'report_ref':ref, 'code':getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED')}))
        raise
    finally:
        stop.set()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    try:
        result = run(args.vault, args.binding)
    except (ContractError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({'ok':False, 'code':getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED'),
            'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
