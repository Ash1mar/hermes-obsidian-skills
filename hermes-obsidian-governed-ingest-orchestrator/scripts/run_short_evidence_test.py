#!/usr/bin/env python3
"""Run the post-MinerU short path on an isolated native Kanban board."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

from fixed_semantic import HermesCaller, decode_semantic_response, prepare_request_projection
from hermes_source_units import ContractError
from hermes_source_units.file_locks import exclusive_lock
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint
from native_stop_inventory import workflow_processes
from orchestration import configuration
from prepare_short_evidence_test import advance, inventory, save, seal
from short_evidence_pass import load_sources, mapping_groups, request, require, seed_requests
from workflow_metrics import usage_for

CONTRACT = 'native-short-evidence-test/v1'
ASSIGNEE = 'ingest-program'
LIMIT = 40_000


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime():
    from watcher_runtime import ensure_native_runtime
    ensure_native_runtime()
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc, kanban_db_dispatch as kd
    return kb, kc, kd


def source_boundary(vault, workflow_id, pins):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', workflow_id), 'invalid origin workflow identity')
    path = _vault_path(vault, f'_system/ledgers/ingest-workflows/{workflow_id}.json')
    before = sha(path)
    origin = _load_json(path)
    require(origin['state'] in ('completed', 'partial') and
            not workflow_processes(workflow_id, origin['kanban']['board_id']), 'origin must be settled and idle')
    sources = load_sources(vault, pins)
    ready = {row['path']: row for row in origin['source_outcomes'] if row['status'] == 'ready'}
    require(len(ready) == len(pins), 'exact ready-source coverage required')
    for pin, source in zip(pins, sources.values(), strict=True):
        row = ready[pin['original']]
        require(row['unit_set_id'] == pin['unit_set_id'] and row['content_sha256'] == source['source_sha256']
                and pin['bundle_manifest'] in row['artifact_refs'], 'source differs from the accepted post-MinerU boundary')
    require(sha(path) == before, 'origin changed during post-MinerU verification')
    return sources, {'workflow_id': workflow_id, 'revision': origin['revision'], 'path': str(path), 'sha256': before,
                     'inventory_sha256': fingerprint(inventory(sources)), 'mineru_calls': 0}


def live(root, operation):
    require(not (root / 'stop.json').exists(), 'experiment stopped')
    require(sha(Path(operation['origin']['path'])) == operation['origin']['sha256'],
            'pinned origin workflow changed')


def task_body(root, job):
    return json.dumps({
        'contract': CONTRACT, 'operation_sha256': sha(root / 'operation.json'),
        'job': job.name, 'job_sha256': sha(job),
    }, sort_keys=True)


def build_call(row, *, diagnostic, slots):
    properties = row['schema']['properties']['results']['properties']
    view = {**row['view'], 'structural_retry': {'reason': diagnostic[:400]}}
    images = row['image_inputs']
    page_call = row['phase'] == 'short-write'
    if page_call:
        view['pages'] = {slot: row['view']['pages'][slot] for slot in slots}
        properties = {slot: properties[slot] for slot in slots}
    return request(row['phase'], view, properties if page_call else properties['r'],
                   row['budget']['estimated_output_tokens'], row['budget']['input_cap'], images, slots=page_call)


def checked_projection(row):
    p = prepare_request_projection(row['phase'], row['view'], row['schema'], row['image_inputs'])
    require(row['budget']['fits'] and p['input_size'] <= row['budget']['input_cap']
            and p['content'] == row['messages'] and p['wire']['system'] == row['system']
            and p['transport_schema'] == row['transport_schema'], 'exact request or budget changed')
    return p


def run_job(root, job, own):
    payload = _load_json(job)
    original = payload['request']
    row = original
    slots = row.get('missing_slots', ['r'])
    attempts = []
    for ordinal in (1, 2):
        if not row['budget']['fits']:
            break
        grant = own()
        p = checked_projection(row)
        p['live_control_check'] = own
        p['audit_context'] = {'short_evidence_root': str(root), 'binding': grant, 'phase': row['phase']}
        reservation = job.with_name(job.stem + f'.attempt-{ordinal}.json')
        require(not reservation.exists(), 'a consumed or unknown ordinal cannot be resent')
        started = time.monotonic()
        wall = time.time()
        caller = HermesCaller()
        caller.input_limit = row['budget']['input_cap']
        caller.output_token_limit = row['output_cap']
        caller.images = row['image_inputs']
        # Persist the exact payload before entering the SDK.
        save(reservation, {'request': row, 'ordinal': ordinal, 'grant': grant, 'started_at': wall})
        audit_path = job.with_name(job.stem + f'.audit-{ordinal}.json')
        audit = {'phase': row['phase'], 'request_id': row['id'], 'ordinal': ordinal, 'grant': grant,
                 'started_at': wall}
        try:
            with caller.request_scope(p):
                raw, model = caller(row['phase'], row['view'], row['schema'])
        except Exception as error:
            audit.update(state='transport_or_runtime_hold', error=str(error), seconds=time.monotonic() - started)
            save(audit_path, audit)
            attempts.append(audit)
            break
        audit.update(raw=raw, model=model, seconds=time.monotonic() - started, state='returned')
        save(audit_path, audit)
        own()
        try:
            response, _ = decode_semantic_response(raw)
            plan = {'requests': [{**original, 'missing_slots': slots}]}
            captured = seal(plan, original['id'], response, root / 'receipts')
        except (ValueError, TypeError, ContractError) as error:
            captured = {'sealed': [], 'held': {slot: str(error) for slot in slots}}
        attempts.append({**audit, 'capture': captured})
        if not captured['held']:
            return {'state': 'settled', 'attempts': attempts}
        slots = list(captured['held'])
        if ordinal == 1:
            row = build_call(original, diagnostic='; '.join(captured['held'].values()), slots=slots)
    return {'state': 'held', 'attempts': attempts, 'unsettled_slots': slots}


def worker(root, job):
    kb, kc, kd = runtime()
    operation = _load_json(root / 'operation.json')
    task_id = os.environ['HERMES_KANBAN_TASK']
    board = os.environ['HERMES_KANBAN_BOARD']
    run = int(os.environ['HERMES_KANBAN_RUN_ID'])
    claim = os.environ['HERMES_KANBAN_CLAIM_LOCK']
    require(board == operation['board'] and job.parent == root / 'jobs', 'invalid native experiment binding')

    def own(*, stopped=False):
        if not stopped:
            live(root, operation)
        with closing(kc.connect(board=board)) as conn:
            task = kb.get_task(conn, task_id)
        require(task and task.created_by == CONTRACT and task.status == 'running'
                and task.current_run_id == run and task.worker_pid == os.getpid()
                and task.claim_lock == claim and task.body == task_body(root, job),
                'native run, claim, PID or request binding changed')
        return {'task_id': task_id, 'run_id': run, 'board': board}

    deadline = time.monotonic() + 10
    while True:
        try:
            own()
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)
    finished = threading.Event()

    def heartbeat():
        while not finished.wait(15):
            try:
                own()
                with closing(kc.connect(board=board)) as conn:
                    require(kd.heartbeat_worker(conn, task_id, expected_run_id=run, note=CONTRACT), 'native claim ended')
            except Exception:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    def stopped(*_):
        raise RuntimeError('native short-path worker stopped')

    signal.signal(signal.SIGTERM, stopped)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = run_job(root, job, own)
        own()
        save(job.with_suffix('.result.json'), {**result, 'task_id': task_id, 'run_id': run, 'request_id': job.stem})
        with closing(kc.connect(board=board)) as conn:
            require(kb.complete_task(conn, task_id, expected_run_id=run, result=json.dumps({
                'state': result['state'], 'result_ref': str(job.with_suffix('.result.json')),
            })), 'native completion acknowledgement failed')
    except Exception as error:
        with closing(kc.connect(board=board)) as conn:
            own(stopped=True)
            kd._record_task_failure(conn, task_id, str(error), outcome='short_evidence_failed',
                                    force_trip=True, release_claim=True, end_run=True)
        raise
    finally:
        finished.set()
        thread.join(timeout=2)


@contextmanager
def drain_on_error(root, conn, kb, kd, active):
    try:
        yield
    except BaseException:
        save(root / 'stop.json', {'operation': CONTRACT, 'reason': 'controller interrupted or stage failed'})
        for proc, _ in active.values():
            if proc.poll() is None:
                proc.terminate()
        for tid, (proc, _) in active.items():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if kb.get_task(conn, tid).status == 'running':
                kd._record_task_failure(conn, tid, 'experiment controller stopped', outcome='short_evidence_stopped',
                                        force_trip=True, release_claim=True, end_run=True)
        raise


def stage(root, operation, rows, capacity):
    kb, kc, kd = runtime()
    board = operation['board']
    pending = []
    for row in rows:
        job = root / 'jobs' / (row['id'] + '.json')
        save(job, {'request': row})
        pending.append(job)
    active, results = {}, []
    native_cap = kd.resolve_max_in_progress(kd.configured_max_in_progress())
    capacity_wait = time.monotonic()
    with closing(kc.connect(board=board)) as conn, drain_on_error(root, conn, kb, kd, active):
        while pending or active:
            live(root, operation)
            for tid, (proc, job) in list(active.items()):
                if proc.poll() is None:
                    continue
                task = kb.get_task(conn, tid)
                result_path = job.with_suffix('.result.json')
                if task.status == 'done' and result_path.exists():
                    result = _load_json(result_path)
                    require(result['task_id'] == tid, 'native completion binding changed')
                    results.append(result)
                else:
                    if task.status == 'running':
                        kd._record_task_failure(conn, tid, 'native worker exited before acknowledgement',
                                                outcome='worker_exited', force_trip=True, release_claim=True, end_run=True)
                    results.append({'state': 'held', 'task_id': tid, 'returncode': proc.returncode,
                                    'request_id': job.stem, 'attempts': []})
                del active[tid]
            while pending and len(active) < capacity:
                pressure = kd._memory_pressure_level()
                if pressure == 'critical' or (pressure == 'elevated' and active):
                    break
                if native_cap is not None and kd.count_running_tasks(conn) + kd.count_running_tasks_other_boards(board) >= native_cap:
                    break
                job = pending.pop(0)
                body = task_body(root, job)
                phase = _load_json(job)['request']['phase']
                tid = kb.create_task(conn, title=f'Short path {phase}', body=body, assignee=ASSIGNEE,
                                     created_by=CONTRACT, workspace_kind='dir', workspace_path=str(root),
                                     project_id='', initial_status='running', max_retries=1,
                                     max_runtime_seconds=1200, idempotency_key=job.stem, board=board)
                task = kb.get_task(conn, tid)
                require(task.body == body and task.status == 'ready', 'existing native run requires explicit recovery')
                claim = kb.claim_task(conn, tid, claimer=CONTRACT)
                require(claim is not None, 'could not claim exact short-path request')
                env = dict(os.environ, HERMES_KANBAN_TASK=tid, HERMES_KANBAN_BOARD=board,
                           HERMES_KANBAN_DB=str(kb.kanban_db_path(board=board)),
                           HERMES_KANBAN_RUN_ID=str(claim.current_run_id), HERMES_KANBAN_CLAIM_LOCK=claim.claim_lock)
                argv = [sys.executable, str(Path(__file__).resolve()), 'worker', '--output', str(root), '--job', str(job)]
                try:
                    with kd._open_worker_log(claim, board) as log:
                        proc = subprocess.Popen(kd._restart_safe_worker_argv(claim, argv), cwd=root, env=env,
                                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                    kd._set_worker_pid(conn, tid, proc.pid)
                    active[tid] = proc, job
                except Exception as error:
                    kd._record_task_failure(conn, tid, str(error), outcome='spawn_failed',
                                            force_trip=True, release_claim=True, end_run=True)
                    raise
            if active:
                capacity_wait = time.monotonic()
            if pending and time.monotonic() - capacity_wait > 120:
                raise RuntimeError('native capacity stayed unavailable for 120 seconds')
            if pending or active:
                time.sleep(.4)
    return results


def stage_metrics(root, rows, results, start, end):
    audit_rows = [attempt for result in results for attempt in result['attempts']]
    ids = {r['id'] for r in rows}
    failures = [_load_json(path) for path in (root / 'call-failures').glob('*.json')]
    task_ids = {result['task_id'] for result in results}
    matching = [row for row in failures if row['task_id'] in task_ids]
    models = [{'model': {'session_id': row['session_id']}} for row in matching]
    models.extend(row for row in audit_rows if row.get('model'))
    from hermes_state import SessionDB
    db = SessionDB(read_only=True)
    database = db.db_path
    db.close()
    usage = usage_for(models, database)
    return {
        'phase': rows[0]['phase'], 'planned_requests': len(ids), 'native_tasks': len(results),
        'started_at': start, 'ended_at': end, 'wall_seconds': end - start,
        'worker_attempts_observed': len(audit_rows),
        'semantic_retries': sum(row['ordinal'] > 1 for row in audit_rows),
        'network_retries_reported': sum(row.get('model', {}).get('network_retries', {}).get('connection_retries', 0)
                                        for row in audit_rows),
        'transport_unknowns': sum(row.get('state') == 'completion_unknown' for row in matching),
        'held_tasks': sum(row['state'] == 'held' for row in results),
        'sdk_attempt_seconds_sum': sum(row['seconds'] for row in audit_rows),
        'usage': usage,
    }


def start(root, vault, workflow_id, pins, operation_id):
    require(not os.environ.get('HERMES_KANBAN_TASK') and not os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),
            'trusted operator must start the experiment')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', operation_id), 'invalid experiment operation ID')
    require(not root.is_relative_to(vault), 'write experiment evidence outside the source Vault')
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / 'controller.lock', timeout=0, marker=False):
        require(not (root / 'operation.json').exists(), 'operation already exists; inspect its durable evidence')
        sources, origin = source_boundary(vault, workflow_id, pins)
        board = 'short-' + operation_id
        kb, kc, kd = runtime()
        from hermes_cli.profiles import profile_exists
        require(not profile_exists(ASSIGNEE), 'reserved program assignee is also a profile')
        require(not any(meta['slug'] == board for meta in kb.list_boards()), 'native test board already exists')
        kb.create_board(board, name='Short evidence ' + operation_id, default_workdir=str(root))
        cfg = configuration()
        operation = {'contract': CONTRACT, 'board': board, 'origin': origin, 'body_limit': LIMIT,
                     'vault': str(vault), 'pins': pins, 'started_at': time.time(),
                     'pass_capacity': cfg['pass_worker_concurrency'], 'page_capacity': cfg['reduce_page_concurrency']}
        save(root / 'operation.json', operation)
        seeds, groups = seed_requests(sources), mapping_groups(sources)
        metrics, holds = [], {}
        while True:
            live(root, operation)
            planning_started = time.monotonic()
            plan = advance(sources, seeds, groups, root / 'receipts', LIMIT, native_holds=holds)
            planning_seconds = time.monotonic() - planning_started
            save(root / f"stage-{len(metrics) + 1:02}-{plan['stage']}.json", plan)
            rows = plan.get('requests', [])
            if not rows:
                final = 'partial' if (plan.get('stage') != 'offline_build' or plan.get('held')
                    or plan.get('coordination_issues') or plan.get('reduced', {}).get('unresolved')
                    or any(check['state'] != 'structurally_valid' for check in plan.get('checks', []))) else 'completed'
                break
            phase = rows[0]['phase']
            capacity = operation['page_capacity'] if phase == 'short-write' else operation['pass_capacity']
            stage_start = time.time()
            results = stage(root, operation, rows, capacity)
            item = stage_metrics(root, rows, results, stage_start, time.time())
            item['prepare_seconds'] = planning_seconds
            metrics.append(item)
            save(root / f"metrics-{len(metrics):02}-{phase}.json", item)
            _, verified = source_boundary(vault, workflow_id, pins)
            require(verified == origin, 'source changed across native stage')
            for row in results:
                if row['state'] == 'held':
                    holds[row['request_id']] = {'task_id': row['task_id'], 'request_id': row['request_id']}
            if results and all(r['state'] == 'held' for r in results):
                final = 'held'
                break
        with closing(kc.connect(board=board)) as conn:
            remaining = [task.id for task in kb.list_tasks(conn) if task.status in ('ready', 'running', 'todo')]
        result = {'state': final, 'stages': metrics, 'last_stage': plan['stage'], 'origin': origin, 'board': board,
                  'native_active_or_ready': remaining, 'mineru_calls': 0, 'independent_build_review_calls': 0,
                  'build_checks': len(plan.get('checks', [])), 'publication_eligible': False,
                  'build_check_seconds': planning_seconds if plan['stage'] == 'offline_build' else None,
                  'execution_holds': holds, 'wall_seconds': time.time() - operation['started_at']}
        save(root / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'worker'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--origin-workflow')
    parser.add_argument('--sources', type=Path)
    parser.add_argument('--operation-id')
    parser.add_argument('--job', type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.action == 'worker':
        worker(root, args.job.resolve(strict=True))
    else:
        start(root, args.vault.resolve(strict=True), args.origin_workflow, _load_json(args.sources), args.operation_id)


if __name__ == '__main__':
    main()
