"""Pull program cards through Hermes' native claims and process registry.

The non-profile assignee is intentionally skipped by the model dispatcher.
Only this trusted workflow reconciler can start its fixed, installed entrypoint.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys

from program_worker import ASSIGNEE, canonical_binding, admission_check
from hermes_source_units import ContractError


def dispatch_programs(adapter, workflow_id):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise RuntimeError('program dispatcher requires a trusted operator process')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc, kanban_db_dispatch as kd, kanban_db_workspace as kw
    from hermes_cli.profiles import profile_exists
    value = adapter.workflow.status(workflow_id)
    if value['cancel_requested'] or adapter.workflow.pending_pause(value):
        return []
    # A profile with this name would allow Gateway to launch a model instead.
    if profile_exists(ASSIGNEE):
        raise RuntimeError('ingest-program is a reserved non-profile assignee')
    board = value['kanban']['board_id']
    if not board:
        return []
    spawned = []
    with contextlib.closing(kc.connect(board=board)) as conn:
        kd.reap_worker_zombies()
        cap = kd.resolve_max_in_progress(kd.configured_max_in_progress())
        pressure = kd._memory_pressure_level()
        for card in value['kanban']['task_map']:
            task = kb.get_task(conn, card['task_id'])
            if not task or task.status != 'ready' or task.assignee != ASSIGNEE:
                continue
            if pressure == 'critical' or (pressure == 'elevated' and spawned):
                break
            if cap is not None and kd.count_running_tasks(conn) + kd.count_running_tasks_other_boards(board) >= cap:
                break
            if kd.check_respawn_guard(conn, task.id) is not None:
                continue
            body = json.loads(task.body)
            executor = body.get('executor')
            if executor not in ('program-v1', 'semantic-v1') or body.get('workflow_id') != workflow_id:
                raise RuntimeError('program card lacks its executor contract')
            request, current = canonical_binding(adapter, body['worker_binding'], executor=executor)
            if request['node'].startswith('pass-slice:'):
                batch=adapter.workflow.knowledge._batch(current['batch_id'])
                occupied={'pass-slice:'+s['slice_id'] for s in adapter.workflow.knowledge._slices(current['batch_id']) if s['lease']['worker_id']}
                # Native claims can precede domain leasing. Reserve that in-flight
                # capacity too, without counting its eventual lease twice.
                occupied.update(c['node'] for c in current['kanban']['task_map'] if c['node'].startswith('pass-slice:')
                    and (native:=kb.get_task(conn,c['task_id'])) and native.status=='running')
                if len(occupied)>=batch['slice_config']['pass_worker_concurrency']:
                    return spawned
            if (request['task_id'] != task.id or request['node'] != card['node']
                    or current['kanban']['board_id'] != board
                    or task.idempotency_key != card['idempotency_key']):
                raise RuntimeError('native program card differs from the canonical binding')
            try:
                admission_check(adapter, request)
            except ContractError as exc:
                if exc.code in ('WORKFLOW_PAUSED', 'WORKFLOW_STOPPED', 'PASS_ADMISSION_WAIT'):
                    return spawned
                if exc.code == 'STALE_INPUT':
                    from ingest_kanban import desired_graph
                    latest = adapter.workflow.status(workflow_id)
                    node = next((n for n in desired_graph(adapter.workflow, latest)
                        if n.name == request['node']), None)
                    if node is None or node.input_fingerprint != request['input_fingerprint']:
                        # A predecessor can commit during the projection tick.
                        # The next trusted sync retires this unclaimed card and
                        # binds the new graph. Never claim/retry the old identity.
                        with kb.write_txn(conn):
                            kb._append_event(conn, task.id, 'program_admission_deferred',
                                {'reason':'projection_changed', 'node':request['node']})
                        return spawned
                # Stable-graph contract failures require actual recovery, not
                # repeated admission or whole-workflow cancellation.
                adapter.execution_failure(request, exc.code, str(exc))
                return spawned
            # Native claim enforces parents, opens a run and uses ready->running CAS.
            claimed = kb.claim_task(conn, task.id, claimer='ingest-program-dispatch')
            if claimed is None:
                continue
            try:
                workspace = kw.resolve_workspace(claimed, board=board)
                kw.set_workspace_path(conn, claimed.id, str(workspace))
                from tools.environments.local import build_subprocess_env
                from gateway.session_context import _VAR_MAP
                env = build_subprocess_env(inherit_profile_home=True)
                for key in _VAR_MAP:
                    env.pop(key, None)
                env.pop('HERMES_DELEGATED_CHILD_CONTEXT', None)
                env.update(HERMES_KANBAN_TASK=claimed.id,
                    HERMES_KANBAN_RUN_ID=str(claimed.current_run_id),
                    HERMES_KANBAN_CLAIM_LOCK=claimed.claim_lock,
                    HERMES_KANBAN_BOARD=board, HERMES_KANBAN_DB=str(kb.kanban_db_path(board=board)),
                    HERMES_KANBAN_WORKSPACES_ROOT=str(kb.workspaces_root(board=board)),
                    HERMES_KANBAN_WORKSPACE=str(workspace), TERMINAL_CWD=str(workspace),
                    HERMES_INGEST_EXECUTOR=executor)
                script = Path(__file__).resolve().parents[1] / 'scripts/run_program_worker.py'
                argv = kd._restart_safe_worker_argv(claimed, [sys.executable, str(script),
                    '--vault', str(adapter.workflow.vault), '--binding', body['worker_binding']])
                with kd._open_worker_log(claimed, board) as output:
                    proc = subprocess.Popen(argv, cwd=workspace, env=env,
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                kd._set_worker_pid(conn, claimed.id, proc.pid)
                if os.name == 'nt':
                    kd._live_worker_procs[proc.pid] = proc
                kb._fire_worker_spawned_hook(conn, claimed, str(workspace), proc.pid, board=board)
                spawned.append(claimed.id)
            except Exception as exc:
                kd._record_task_failure(conn, claimed.id, str(exc), outcome='spawn_failed',
                    failure_limit=kd.DEFAULT_FAILURE_LIMIT, release_claim=True, end_run=True)
                adapter.execution_failure(request, 'PROGRAM_SPAWN_FAILED', str(exc))
    return spawned
