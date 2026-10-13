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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'hermes-obsidian-controlled-ingest/lib'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes, _write_atomic
from ingest_kanban import IngestKanbanAdapter
from program_worker import canonical_binding, execute
from hermes_source_units.runtime_telemetry import measured, span, notify_reconciler


def cleanup_diagnostic(message):
    """A reporting failure must not replace the worker's original failure."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


@measured('worker_lifecycle', lambda vault, binding: {'vault':vault})
def run(vault, binding):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc, kanban_db_dispatch as kd
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    executor = os.environ.get('HERMES_INGEST_EXECUTOR')
    if executor not in ('program-v1','semantic-v1'):
        raise RuntimeError('unsupported native executor grant')
    request, value = canonical_binding(adapter, binding, executor=executor)
    if (os.environ.get('HERMES_INGEST_EXECUTOR') != executor
            or os.environ.get('HERMES_KANBAN_TASK') != request['task_id']
            or os.environ.get('HERMES_KANBAN_BOARD') != value['kanban']['board_id']):
        raise RuntimeError('program execution requires its native dispatcher grant')
    run_id = int(os.environ['HERMES_KANBAN_RUN_ID'])
    board = value['kanban']['board_id']
    registered_pid = None
    def own_run():
        nonlocal registered_pid
        with contextlib.closing(kc.connect(board=board)) as conn:
            task = kb.get_task(conn, request['task_id'])
            owned = (task is not None and task.status == 'running' and task.current_run_id == run_id
                and task.claim_lock == os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
                and bool(task.worker_pid))
            if owned and registered_pid is None:
                registered_pid = task.worker_pid
            return owned
    # Do not write domain state before the parent persists our process identity.
    deadline = time.monotonic() + 10
    while not own_run():
        if time.monotonic() >= deadline:
            raise RuntimeError('native run registration missing or superseded')
        time.sleep(.05)
    grant = {'claim_lock':os.environ.get('HERMES_KANBAN_CLAIM_LOCK'), 'worker_pid':registered_pid}
    stop = threading.Event()
    completed = threading.Event()
    def heartbeat():
        while not stop.wait(30):
            try:
                with contextlib.closing(kc.connect(board=board)) as conn:
                    alive = own_run() and kd.heartbeat_worker(conn, request['task_id'], expected_run_id=run_id,
                        note='fixed semantic executor' if executor=='semantic-v1' else 'program executor; no model session')
                if alive and executor=='semantic-v1' and request['node'].startswith('pass-slice:') and not completed.is_set():
                    from fixed_semantic import heartbeat as pass_heartbeat
                    pass_heartbeat(vault)
            except Exception:
                alive = False
            if not alive:
                if not completed.is_set():
                    os.kill(os.getpid(), signal.SIGTERM)
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    def terminated(signum,frame):
        raise ContractError('NATIVE_WORKER_STOPPED','$','native program was stopped; preserve valid partial results')
    previous_handler=signal.signal(signal.SIGTERM,terminated)
    thread.start()
    progress = Path(binding).with_suffix('.execution.json')
    try:
        _write_atomic(progress, _json_bytes({'state':'running', 'executor':executor, 'run_id':run_id,'grant':grant}))
        if executor=='semantic-v1':
            from fixed_semantic import execute as semantic_execute
            result=semantic_execute(adapter,binding)
        else:
            result = execute(adapter, binding)
        completed.set()
        with span('domain_completion_receipt',binding=request,domain_ok=bool(result.get('ok'))):
            _write_atomic(progress, _json_bytes({'state':'completed' if result.get('ok') else 'blocked',
                'executor':executor, 'run_id':run_id, 'grant':grant, 'result':result}))
        notify_reconciler(vault,request['workflow_id'])
        # The trusted reconciler acknowledges the durable result and holds
        # successors at pauses. Keep the native PID alive until it does so.
        with span('completion_acknowledgement',binding=request):
            deadline = time.monotonic() + 120
            while own_run() and time.monotonic() < deadline:
                time.sleep(.5)
        return result
    except Exception as exc:
        completed.set()
        # Record the cause. The trusted reconciler settles this exact ended
        # native run's lease; failure cleanup does not depend on live input
        # checks or compete with the worker heartbeat for a new revision.
        try:
            ref = adapter.execution_failure(request, getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED'), str(exc))
            _write_atomic(progress, _json_bytes({'state':'execution_blocked', 'executor':executor,
                'run_id':run_id, 'grant':grant, 'report_ref':ref, 'code':getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED')}))
        except Exception as report_error:
            cleanup_diagnostic('Worker failure reporting deferred: ' + str(report_error))
        raise
    finally:
        try:
            stop.set()
            thread.join(timeout=2)
            signal.signal(signal.SIGTERM,previous_handler)
        finally:
            # The native helper cannot safely combine an exact grant check with
            # its private transaction here. Exit normally and let the trusted
            # CLI reap the ended run and settle only its matching domain lease.
            # Notify last, even when failure reporting could not be persisted.
            try:
                notify_reconciler(vault,request['workflow_id'])
            except Exception as wake_error:
                cleanup_diagnostic('Worker cleanup wake deferred: ' + str(wake_error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    try:
        result = run(args.vault, args.binding)
    except Exception as exc:
        print(json.dumps({'ok':False, 'code':getattr(exc, 'code', 'PROGRAM_EXECUTION_FAILED'),
            'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
