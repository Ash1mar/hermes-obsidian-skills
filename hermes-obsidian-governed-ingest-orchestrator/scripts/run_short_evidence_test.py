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
from post_mineru_test import bootstrap_source_boundary, imported_source_boundary, verify_import_board
from prepare_short_evidence_test import advance, inventory, save, seal
from short_evidence_pass import load_sources, mapping_groups, request, require, seed_requests
from short_page_contract import CONTRACT as FRAGMENT_CONTRACT, WRITE_CONTRACT, retry_slots, writing_retry
from workflow_metrics import usage_for

CONTRACT = 'native-short-evidence-test/v1'
ASSIGNEE = 'ingest-program'
LIMIT = 40_000
NODE_ORDER = {'mapping': 0, 'coordination': 1, 'writing': 2}
STAGE_NODE = {
    'map': 'mapping', 'local-map': 'mapping',
    'coordination': 'coordination', 'coordination_held': 'coordination',
    'local_coordination': 'coordination', 'local_page_detail': 'coordination',
    'writing': 'writing', 'offline_build': 'writing',
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime():
    from watcher_runtime import ensure_native_runtime
    ensure_native_runtime()
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc, kanban_db_dispatch as kd
    return kb, kc, kd


def source_boundary(vault, workflow_id, pins, registry_revision=None):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', workflow_id), 'invalid origin workflow identity')
    path = _vault_path(vault, f'_system/ledgers/ingest-workflows/{workflow_id}.json')
    before = sha(path)
    origin = _load_json(path)
    terminal = origin['state'] in ('completed', 'partial')
    if not terminal:
        fresh = bootstrap_source_boundary(vault, workflow_id, origin)
        require(fresh == pins if fresh else imported_source_boundary(vault, workflow_id, origin),
                'origin must be a verified source handoff')
        verify_import_board(origin['kanban']['board_id'])
    require((terminal or origin.get('pause_control', {}).get('boundary') == 'exact_plan') and
            not workflow_processes(workflow_id, origin['kanban']['board_id']), 'origin must be settled and idle')
    sources = load_sources(vault, pins)
    if registry_revision is not None:
        # Access and registered material were just verified against current
        # governance. Only the historical model-envelope revision is replayed.
        require(type(registry_revision) is int and registry_revision >= 0
                and all(registry_revision <= s['registry_revision'] for s in sources.values()),
                'invalid historical source envelope revision')
        sources = {key: {**source, 'registry_revision': registry_revision} for key, source in sources.items()}
    ready = {row['path']: row for row in origin['source_outcomes'] if row['status'] == 'ready'}
    require(len(ready) == len(pins), 'exact ready-source coverage required')
    for pin, source in zip(pins, sources.values(), strict=True):
        row = ready[pin['original']]
        require(row['unit_set_id'] == pin['unit_set_id'] and row['content_sha256'] == source['source_sha256']
                and pin['bundle_manifest'] in row['artifact_refs'], 'source differs from the accepted post-MinerU boundary')
    require(sha(path) == before, 'origin changed during post-MinerU verification')
    return sources, {'workflow_id': workflow_id, 'revision': origin['revision'], 'path': str(path), 'sha256': before,
                     'inventory_sha256': fingerprint(inventory(sources)), 'mineru_calls': 0}


def live(root, operation, *, resumed=False, entry=None):
    stop = root / 'stop.json'
    if entry:
        control = _load_json(root / 'reentries' / entry / 'control.json')
        require(control['entry'] == entry and control['operation_sha256'] == sha(root / 'operation.json')
                and not (root / 'reentries' / entry / 'stop.json').exists(),
                'node reentry authority changed or stopped')
        if control.get('fragment_exclusions'):
            evidence = control['fragment_exclusions']['evidence']
            require(sha(_vault_path(root.parent, evidence['ref'])) == evidence['sha256'],
                    'fragment exclusion evidence changed')
    elif resumed:
        require(_load_json(root / 'resume.json')['stop_sha256'] == sha(stop)
                and not (root / 'stop-resume.json').exists(), 'resumed experiment stopped or authority changed')
    else:
        require(not stop.exists(), 'experiment stopped')
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


def checked_retry(prior, call, job, audit=None):
    retry = call['view']['structural_retry']
    slots = list(call['view']['pages']) if call['phase'] == 'short-write' else ['r']
    if 'slots' not in retry:
        return build_call(prior, diagnostic=retry['reason'], slots=slots) == call
    first = _load_json(job.with_name(job.stem + '.audit-1.json'))
    completion = _load_json(job.with_suffix('.result.json'))
    captured = completion['attempts'][0]['capture']['held']
    drafts = response_drafts(first['raw'])
    locked = any('locked_draft_sha256' in spec for spec in retry['slots'].values())
    if locked:
        reservation = _load_json(job.with_name(job.stem + '.attempt-1.json'))
        if (audit is None or first['state'] != 'returned' or first['ordinal'] != 1
                or first['grant'] != reservation['grant'] or first['grant'] != audit['grant']
                or reservation['ordinal'] != 1 or first['request_id'] != prior['id']
                or reservation['request'] != prior or not authentic_reply(first, prior)):
            return False
    return (set(captured) == set(slots)
            and writing_retry(prior, slots, drafts, captured, inspection_patch=locked) == call)


def authentic_reply(audit, call):
    from fixed_semantic_recovery import transcript_reply

    model, grant = audit['model'], audit['grant']
    if model['parent_native_run_id'] != str(grant['run_id']) or not model['fixed_request_checks']:
        return False
    wire = checked_projection(call)['wire']
    proof = {'model': model, 'response_sha256': fingerprint(audit['raw']), 'user_sha256': fingerprint(wire['user']),
             'image_inputs': [{k: v for k, v in img.items() if k != 'url'} for img in call['image_inputs']]}
    return transcript_reply(proof, wire) == audit['raw']


def locked_drafts(job):
    return response_drafts(_load_json(job.with_name(job.stem + '.audit-1.json'))['raw'])


def response_drafts(raw):
    try:
        answer, _ = decode_semantic_response(raw)
        if isinstance(answer, dict) and isinstance(answer.get('results'), dict):
            return answer['results']
    except (TypeError, ValueError, ContractError):
        pass
    return {}


def checked_projection(row):
    p = prepare_request_projection(row['phase'], row['view'], row['schema'], row['image_inputs'])
    body = {k: row[k] for k in ('contract', 'phase', 'messages', 'system', 'schema', 'transport_schema', 'output_cap')}
    require(fingerprint(body) == row['id'] and row['budget']['fits'] and p['input_size'] <= row['budget']['input_cap']
            and p['content'] == row['messages'] and p['wire']['system'] == row['system']
            and p['transport_schema'] == row['transport_schema'], 'exact request or budget changed')
    return p


def run_job(root, job, own):
    payload = _load_json(job)
    original = payload['request']
    if original['phase'] in ('short-formal-build', 'short-formal-qa'):
        from short_formal_build import build
        return build(root, job, original, own, sys.modules[__name__])
    if 'saved_attempts' in payload:
        overlay = None
        if payload.get('entry') and original['phase'] == 'short-map':
            directory = root / 'reentries' / payload['entry']
            control = _load_json(directory / 'control.json')
            if original['id'] in control.get('mapping_recovery', {}).get('requests', []):
                overlay = directory / 'mapping-receipts'
        return replay_node_output(root, original, payload['saved_attempts'], own, mapping_overlay=overlay)
    if payload.get('replay'):
        return replay_mapping(root, original, own)
    profile = _load_json(root / 'operation.json').get('transport_profile', 'shared')
    if original['view'].get('fragment_repair') == 'short-content-recovery/v1':
        from short_content_recovery import slot_attempts
        require(all(len(slot_attempts(root, view['repair_of_fragment'])) == 1
                    for view in original['view']['pages'].values()),
                'content recovery allowance already consumed or original completion unknown')
    row = original
    slots = row.get('missing_slots', ['r'])
    attempts = []
    drafts = {}
    ordinals = (1,) if original['view'].get('fragment_repair') else (1, 2)
    retry_budget = None
    for ordinal in ordinals:
        if not row['budget']['fits']:
            retry_budget = row['budget']
            break
        grant = own()
        p = checked_projection(row)
        p['live_control_check'] = own
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
        p['audit_context'] = {'short_evidence_root': str(root), 'binding': grant, 'phase': row['phase'],
                              'reservation': {'ref': reservation.relative_to(root).as_posix(), 'sha256': sha(reservation),
                                              'ordinal': ordinal, 'request_sha256': row['id']},
                              'transport_profile': profile}
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
            captured = seal(plan, original['id'], response, root / 'receipts', call=row, drafts=drafts)
        except (ValueError, TypeError, ContractError) as error:
            captured = {'sealed': [], 'held': {slot: str(error) for slot in slots}}
        attempts.append({**audit, 'capture': captured})
        if not captured['held']:
            return {'state': 'settled', 'attempts': attempts}
        slots = list(captured['held'])
        if ordinal < ordinals[-1]:
            if original['view'].get('writing_contract') == WRITE_CONTRACT:
                drafts = response_drafts(raw)
                row = writing_retry(original, slots, drafts, captured['held'], inspection_patch=True)
            else:
                row = build_call(original, diagnostic='; '.join(captured['held'].values()), slots=slots)
    return {'state': 'held', 'attempts': attempts, 'unsettled_slots': slots,
            **({'retry_budget_hold': retry_budget} if retry_budget else {})}


def replay_mapping(root, row, own):
    """Native read-only proof replay. It never constructs a model caller."""
    from fixed_semantic_recovery import transcript_reply
    require(row['phase'] == 'short-map', 'mapping-only recovery')
    kb, kc, _ = runtime()
    grant = own()
    old_job = root / 'jobs' / (row['id'] + '.json')
    reserved = _load_json(old_job.with_name(old_job.stem + '.attempt-1.json'))
    audit_path = old_job.with_name(old_job.stem + '.audit-1.json')
    audit = _load_json(audit_path)
    require(_load_json(old_job)['request'] == row and reserved['request'] == row
            and audit['request_id'] == row['id'] and audit['state'] == 'returned'
            and audit['grant'] == reserved['grant'], 'saved native mapping does not match the original reservation')
    old = audit['grant']
    with closing(kc.connect(board=grant['board'])) as conn:
        prior = kb.get_task(conn, old['task_id'])
        runs = kb.list_runs(conn, old['task_id'], include_active=False)
    require(old['board'] == grant['board'] and prior.created_by == CONTRACT and prior.body == task_body(root, old_job)
            and any(run.id == old['run_id'] for run in runs), 'saved mapping lacks a matching ended native claim')
    projection = checked_projection(row)
    model = audit['model']
    require(model['parent_native_run_id'] == str(old['run_id']) and model['fixed_request_checks'] > 0,
            'saved model response does not bind the old native worker')
    wire = projection['wire']
    proof = {'model': model, 'response_sha256': fingerprint(audit['raw']), 'user_sha256': fingerprint(wire['user']),
             'image_inputs': [{k: v for k, v in image.items() if k != 'url'} for image in row['image_inputs']]}
    require(transcript_reply(proof, wire) == audit['raw'], 'native transcript no longer matches the exact response')
    answer, _ = decode_semantic_response(audit['raw'])
    own()
    seal({'requests': [row]}, row['id'], answer, root / 'receipts')
    return {'state': 'settled', 'attempts': [], 'replayed_from': str(audit_path.relative_to(root)),
            'source_audit_sha256': sha(audit_path), 'model_calls': 0}


def replay_node_output(root, row, saved, own, *, mapping_overlay=None):
    """Reenter consumed requests using only authentic returned responses."""
    from fixed_semantic_recovery import transcript_reply
    kb, kc, _ = runtime()
    sources = []
    remaining = row.get('missing_slots', ['r'])
    for ref in saved:
        own_grant = own()
        old_job = _vault_path(root, ref['job'])
        reservation = _vault_path(root, ref['reservation'])
        audit_path = _vault_path(root, ref['audit'])
        require(sha(old_job) == ref['job_sha256'] and sha(reservation) == ref['reservation_sha256']
                and sha(audit_path) == ref['audit_sha256'], 'saved response proof changed')
        prior_row = _load_json(old_job)['request']
        reserved = _load_json(reservation)
        audit = _load_json(audit_path)
        old = audit['grant']
        call = reserved['request']
        require(prior_row['id'] == row['id'] and call['phase'] == row['phase']
                and audit['request_id'] == call['id'] and old == reserved['grant']
                and old['board'] == own_grant['board'] and audit['state'] == 'returned',
                'replay request differs from native reservation')
        if call['id'] != row['id']:
            require(checked_retry(prior_row, call, old_job, audit),
                    'saved retry differs from the bounded original request')
        with closing(kc.connect(board=old['board'])) as conn:
            prior_task = kb.get_task(conn, old['task_id'])
            closed = kb.list_runs(conn, old['task_id'], include_active=False)
        require(prior_task.created_by == CONTRACT and prior_task.body == task_body(root, old_job)
                and any(run.id == old['run_id'] for run in closed), 'replay requires a closed native worker run')
        projection = checked_projection(call)
        model = audit['model']
        require(model['parent_native_run_id'] == str(old['run_id']) and model['fixed_request_checks'] > 0,
                'saved response does not bind the original SDK call')
        wire = projection['wire']
        proof = {'model': model, 'response_sha256': fingerprint(audit['raw']), 'user_sha256': fingerprint(wire['user']),
                 'image_inputs': [{k: v for k, v in image.items() if k != 'url'} for image in call['image_inputs']]}
        require(transcript_reply(proof, wire) == audit['raw'], 'replay does not match the native transcript')
        try:
            response, _ = decode_semantic_response(audit['raw'])
            if mapping_overlay is not None:
                from short_evidence_pass import mapping_receipt
                record = mapping_receipt(row, response, {**ref, 'grant': old, 'ordinal': reserved['ordinal']})
                save(mapping_overlay / (row['id'] + '.json'), record)
                captured = {'sealed': ['r'], 'held': {}, 'mapping_issues': record['issues']}
            else:
                drafts = locked_drafts(old_job) if any('locked_draft_sha256' in spec
                                                      for spec in retry_slots(call).values()) else {}
                captured = seal({'requests': [{**row, 'missing_slots': remaining}]}, row['id'], response,
                                root / 'receipts', call=call, drafts=drafts)
        except (ValueError, TypeError, ContractError) as error:
            captured = {'held': dict.fromkeys(remaining, str(error)), 'sealed': []}
        sources.append({'audit': ref['audit'], 'capture': captured})
        remaining = list(captured['held'])
        if not remaining:
            break
    return {'state': 'held' if remaining else 'settled', 'attempts': [], 'replays': sources,
            'unsettled_slots': remaining, 'model_calls': 0}


def worker(root, job):
    kb, kc, kd = runtime()
    operation = _load_json(root / 'operation.json')
    task_id = os.environ['HERMES_KANBAN_TASK']
    board = os.environ['HERMES_KANBAN_BOARD']
    run = int(os.environ['HERMES_KANBAN_RUN_ID'])
    claim = os.environ['HERMES_KANBAN_CLAIM_LOCK']
    require(board == operation['board'] and job.parent == root / 'jobs', 'invalid native experiment binding')
    payload = _load_json(job)
    resumed = payload.get('resumed', False)
    entry = payload.get('entry')

    def own(*, stopped=False):
        if not stopped:
            live(root, operation, resumed=resumed, entry=entry)
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
        save(job.with_suffix('.result.json'), {**result, 'task_id': task_id, 'run_id': run,
                                               'request_id': _load_json(job)['request']['id']})
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
def drain_on_error(root, conn, kb, kd, active, resumed, entry):
    try:
        yield
    except BaseException:
        marker = (f'reentries/{entry}/stop.json' if entry else 'stop-resume.json' if resumed else 'stop.json')
        save(root / marker, {'operation': CONTRACT, 'reason': 'controller interrupted or stage failed'})
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


def stage(root, operation, rows, capacity, *, resumed=False, replay=False, entry=None, saved_attempts=None):
    kb, kc, kd = runtime()
    board = operation['board']
    pending = []
    for row in rows:
        prefix = f'entry-{entry}-' if entry else 'replay-' if replay else ''
        name = prefix + row['id']
        job = root / 'jobs' / (name + '.json')
        payload = {'request': row}
        if resumed:
            payload.update(resumed=True, replay=replay)
        if entry:
            payload['entry'] = entry
            if saved_attempts and row['id'] in saved_attempts:
                payload['saved_attempts'] = saved_attempts[row['id']]
        save(job, payload)
        pending.append(job)
    active, results = {}, []
    native_cap = kd.resolve_max_in_progress(kd.configured_max_in_progress())
    capacity_wait = time.monotonic()
    with closing(kc.connect(board=board)) as conn, drain_on_error(root, conn, kb, kd, active, resumed, entry):
        while pending or active:
            live(root, operation, resumed=resumed, entry=entry)
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
                                    'request_id': _load_json(job)['request']['id'], 'attempts': []})
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


def resume_mapping(root, operation, sources, seeds, groups):
    kb, kc, _ = runtime()
    stop = root / 'stop.json'
    board = operation['board']
    require(stop.is_file() and not (root / 'resume.json').exists()
            and not workflow_processes(operation['origin']['workflow_id'], board), 'exact drained stop required')
    with closing(kc.connect(board=board)) as conn:
        require(all(task.status not in ('ready', 'running', 'todo', 'triage') and not task.claim_lock
                    for task in kb.list_tasks(conn)), 'native board has live claims')
    observed = _load_json(root / 'stage-02-map.json')
    plan = advance(sources, seeds, groups, root / 'receipts', LIMIT, page_namespace=operation.get('page_namespace'))
    require(plan['stage'] == 'map' and all(row in observed['requests'] for row in plan['requests']),
            'recovery requires unchanged original mapping requests')
    results = []
    for row in observed['requests']:
        stem = row['id']
        job = root / 'jobs' / (stem + '.json')
        attempts = [_load_json(p) for p in sorted(job.parent.glob(stem + '.audit-*.json'))]
        reserved = _load_json(job.with_name(stem + '.attempt-1.json'))
        result = job.with_suffix('.result.json')
        results.append({'task_id': reserved['grant']['task_id'], 'attempts': attempts,
                        'state': _load_json(result)['state'] if result.is_file() else 'held'})
    item = stage_metrics(root, observed['requests'], results,
                         (root / 'stage-02-map.json').stat().st_mtime, stop.stat().st_mtime)
    item.update(state='interrupted', prepare_seconds=0)
    save(root / 'metrics-02-short-map.json', item)
    save(root / 'resume.json', {'stop_sha256': sha(stop), 'replay_request_ids': [row['id'] for row in plan['requests']]})
    began = time.monotonic()
    replay = stage(root, operation, plan['requests'], operation['pass_capacity'], resumed=True, replay=True)
    save(root / 'mapping-replay.json', {'results': replay, 'seconds': time.monotonic() - began, 'model_calls': 0})
    require(all(row['state'] == 'settled' for row in replay), 'native replay did not validate every affected request')
    return [_load_json(root / 'metrics-01-short-topics.json'), item]


def reentry_node(plan, node):
    current = STAGE_NODE.get(plan['stage'])
    require(current is not None and NODE_ORDER[current] >= NODE_ORDER[node],
            f'{node} requires complete upstream receipts; current stage is {plan["stage"]}')
    return current


def attempt_index(root):
    """Find reserved ordinals independently of native completion markers."""
    previous = {}
    for path in sorted((root / 'jobs').glob('*.attempt-*.json')):
        name, ordinal = path.name.split('.attempt-', 1)
        job = path.with_name(name + '.json')
        row = _load_json(job)['request']
        collected = previous.setdefault(row['id'], [])
        audit = path.with_name(name + '.audit-' + ordinal)
        if audit.is_file() and _load_json(audit)['state'] == 'returned':
            collected.append({
                'job': job.relative_to(root).as_posix(), 'job_sha256': sha(job),
                'reservation': path.relative_to(root).as_posix(), 'reservation_sha256': sha(path),
                'audit': audit.relative_to(root).as_posix(), 'audit_sha256': sha(audit),
            })
    return previous


def mapping_comparison(root, prior, operation, sources, seeds, groups, fences=()):
    """Use authenticated topic receipts and fence spent unknown mapping packets."""
    from short_evidence_pass import checked_answer, map_requests, receipt

    original = _load_json(prior / 'operation.json')
    result = _load_json(prior / 'result.json')
    require(original['origin'] == operation['origin'] and original['pins'] == operation['pins']
            and original['mapping_contract'] == operation['mapping_contract']
            and result['last_stage'] == 'offline_build' and not result['native_active_or_ready'],
            'mapping comparison requires a settled run of the exact source boundary')
    kb, kc, _ = runtime()
    board = original['board']
    require(not workflow_processes(operation['origin']['workflow_id'], board), 'prior native workers still active')
    previous = attempt_index(prior)
    accepted, proofs = {}, {}
    with closing(kc.connect(board=board)) as conn:
        for seed in seeds:
            rid = seed['id']
            path = prior / 'receipts' / f'{rid}.json'
            saved = _load_json(path)
            checked_answer(seed, saved)
            for proof in previous.get(rid, []):
                job, reservation, audit = (_vault_path(prior, proof[k]) for k in ('job', 'reservation', 'audit'))
                reserved, reply = _load_json(reservation), _load_json(audit)
                grant = reserved['grant']
                task = kb.get_task(conn, grant['task_id'])
                if (not task or task.status != 'done' or task.claim_lock or task.body != task_body(prior, job)
                        or not any(run.id == grant['run_id'] for run in kb.list_runs(conn, task.id, include_active=False))
                        or grant != reply['grant'] or _load_json(job)['request'] != seed
                        or not authentic_reply(reply, reserved['request'])):
                    continue
                answer, _ = decode_semantic_response(reply['raw'])
                if receipt(seed, answer) == saved:
                    proofs[rid] = {**proof, 'receipt_sha256': sha(path)}
                    accepted[rid] = saved
                    break
            require(rid in proofs, 'topic receipt lacks acknowledged native provenance')
    rows = map_requests(sources, groups, seeds, accepted, operation['mapping_contract'])
    holds = dict(result['execution_holds'])
    for folder in fences:
        fenced = _load_json(folder / 'result.json')
        require(fenced['origin'] == operation['origin'] and not fenced['native_active_or_ready'],
                'comparison fence requires a stopped run of the same source boundary')
        holds.update(fenced['execution_holds'])
    cohort = [row['id'] for row in rows if row['id'] not in holds]
    for row in rows:
        rid = row['id']
        if rid in cohort:
            checked_answer(row, _load_json(prior / 'receipts' / f'{rid}.json'))
    require(cohort, 'no previously settled mapping packets available for a matched comparison')
    for rid, saved in accepted.items():
        save(root / 'receipts' / f'{rid}.json', saved)
    save(root / 'mapping-comparison.json', {'source_operation_sha256': sha(prior / 'operation.json'),
         'source_result_sha256': sha(prior / 'result.json'), 'source_board': board, 'topic_receipts': proofs,
         'cohort': cohort, 'held_prior_mapping': {r['id']: holds[r['id']] for r in rows if r['id'] in holds},
         'fence_result_hashes': [sha(folder / 'result.json') for folder in fences],
         'transport_profile': operation['transport_profile']})
    return holds


def verify_revision_drafts(root, board, repair):
    kb, kc, _ = runtime()
    for change in repair['changes']:
        if change['action'] != 'revise':
            continue
        proof = change['prior_attempt']
        job, reservation, audit_path = (_vault_path(root, proof[key]) for key in ('job', 'reservation', 'audit'))
        reserved, audit = _load_json(reservation), _load_json(audit_path)
        call, grant = reserved['request'], audit['grant']
        with closing(kc.connect(board=board)) as conn:
            task = kb.get_task(conn, grant['task_id'])
            runs = kb.list_runs(conn, grant['task_id'], include_active=False)
        require(grant['board'] == board and task and task.created_by == CONTRACT and task.body == task_body(root, job)
                and any(run.id == grant['run_id'] for run in runs),
                'revision draft must belong to a closed native writing attempt')
        require(authentic_reply(audit, call), 'revision draft differs from native transcript')


def reenter(root, operation, sources, seeds, groups, node, entry, *, dry_run, page_contract, fragment_repair=None,
            fragment_exclusions=None, mapping_recovery=None, registry_revision=None, inherited=None,
            split_output=None, replay_only=False, content_recovery=False):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', entry), 'valid stable reentry ID required')
    kb, kc, _ = runtime()
    board = operation['board']
    require(not workflow_processes(operation['origin']['workflow_id'], board), 'node reentry requires drained workers')
    with closing(kc.connect(board=board)) as conn:
        require(all(task.status not in ('ready', 'running', 'todo', 'triage') and not task.claim_lock
                    for task in kb.list_tasks(conn)), 'node reentry requires a stopped native board')
    directory = root / 'reentries' / entry
    require(not directory.exists(), 'reentry ID already used')
    if fragment_exclusions:
        evidence = fragment_exclusions['evidence']
        require(set(evidence) == {'ref', 'sha256'}
                and sha(_vault_path(root.parent, evidence['ref'])) == evidence['sha256'],
                'fragment exclusion evidence changed')
    previous = attempt_index(root)
    holds = {}
    if content_recovery:
        require(node == 'writing' and not any((fragment_repair, replay_only, mapping_recovery)),
                'content recovery requires its own writing entry')
        results = [p for p in [root / 'result.json', *root.glob('reentries/*/result.json')]
                   if _load_json(p).get('last_stage') == 'offline_build']
        previous_result = max((_load_json(p) for p in results), key=lambda r: len(r.get('stages', [])))
        require(not previous_result['native_active_or_ready'] and previous_result['origin'] == operation['origin'],
                'content recovery requires a settled native checkpoint')
        holds = previous_result['execution_holds']
    overlay = None
    preserved = []
    if replay_only:
        original = _load_json(root / 'result.json')
        require(node == 'writing' and original['last_stage'] == 'offline_build'
                and original['origin'] == operation['origin'] and not original['native_active_or_ready']
                and not any((fragment_repair, fragment_exclusions, mapping_recovery, inherited)),
                'zero-call writing replay requires the settled original writing checkpoint')
        holds = {rid: hold for rid, hold in original['execution_holds'].items()
                 if not previous.get(rid) or _load_json(root / previous[rid][0]['job'])['request']['phase'] != 'short-write'}
    if node == 'coordination' and operation.get('card_layout', {}).get('content_outcome_recovery'):
        content_recovery = True
    if inherited:
        predecessor = root / 'reentries' / inherited['entry']
        prior_result = _load_json(predecessor / 'result.json')
        require(prior_result['last_stage'] == 'offline_build' and not prior_result['native_active_or_ready']
                and prior_result['origin'] == operation['origin'], 'writing requires a settled upstream entry')
        overlay_entry = inherited.get('mapping_overlay_entry', inherited['entry'])
        overlay = root / 'reentries' / overlay_entry / 'mapping-receipts'
        require(overlay.is_dir(), 'accepted mapping overlay missing')
        holds = prior_result['execution_holds']
        preserved = inherited['preserved_pages']
    if mapping_recovery is not None:
        require(node == 'mapping' and not operation.get('mapping_contract')
                and set(mapping_recovery) == {'requests', 'prior_build'},
                'legacy mapping recovery requires the mapping node and an exact prior Build')
        from hermes_source_units.short_build import evidence
        from hermes_source_units.vault_finalize import FileVaultFinalizeService
        service = FileVaultFinalizeService(operation['vault'])
        prior_build = _load_json(service.build._run_path(mapping_recovery['prior_build']))
        require(prior_build['state'] == 'completed', 'prior short Build must be complete')
        prior_scope = evidence(service.build, prior_build)['scope']
        require(prior_scope['origin'] == operation['origin'], 'prior Build belongs to another short operation')
        preserved = sorted(r['short_id'] for r in prior_scope['pages'])
        original = _load_json(root / 'result.json')
        ids = mapping_recovery['requests']
        require(ids and len(ids) == len(set(ids)) and set(ids) <= original['execution_holds'].keys()
                and all(previous.get(rid) for rid in ids), 'mapping recovery must bind returned prior holds')
        holds = {rid: hold for rid, hold in original['execution_holds'].items() if rid not in ids}
        overlay = directory / 'mapping-receipts'
    plan = advance(sources, seeds, groups, root / 'receipts', LIMIT, page_contract=page_contract,
                   fragment_repair=fragment_repair, fragment_exclusions=fragment_exclusions,
                   mapping_contract=operation.get('mapping_contract'), mapping_overlay=overlay,
                   preserved_pages=preserved, native_holds=holds, writing_contract=operation.get('writing_contract'),
                   split_output=split_output, page_namespace=operation.get('page_namespace'),
                   card_layout=operation.get('card_layout'))
    current = reentry_node(plan, node)
    if mapping_recovery is not None:
        require(plan['stage'] == 'map' and {r['id'] for r in plan['requests']} == set(mapping_recovery['requests']),
                'mapping recovery differs from the pinned affected mapping requests')
    if fragment_repair is not None:
        original = plan.get('fragment_repair', {}).get('original_requests', [])
        require(original and set(original) <= previous.keys(), 'repair requires original reserved writing requests')
        verify_revision_drafts(root, board, fragment_repair)
    pending = plan.get('requests', [])
    if replay_only:
        require(all(row['phase'] == 'short-write' and previous.get(row['id']) for row in pending),
                'writing replay requires exact returned native attempts')
    control = {
        'entry': entry, 'from_node': node, 'effective_node': current, 'stage': plan['stage'],
        'page_contract': page_contract,
        'operation_sha256': sha(root / 'operation.json'),
        'upstream_receipts': {p.name: sha(p) for p in sorted((root / 'receipts').glob('*.json'))},
        'pending_requests': [row['id'] for row in pending],
        'prior_reservations': {row['id']: previous[row['id']] for row in pending if row['id'] in previous},
        'started_at': time.time(),
    }
    if replay_only:
        control['replay_only'] = True
    if content_recovery:
        control['content_outcome_recovery'] = True
    if fragment_repair is not None:
        control['fragment_repair'] = fragment_repair
        control['fragment_repair_preview'] = plan['fragment_repair']
    if fragment_exclusions is not None:
        control['fragment_exclusions'] = fragment_exclusions
    if split_output:
        control['split_output'] = split_output
        control['output_split_preview'] = plan.get('output_split')
    if mapping_recovery is not None:
        control['mapping_recovery'] = mapping_recovery
        control['preserved_pages'] = preserved
        control['registry_revision'] = registry_revision
    if inherited:
        control.update(mapping_recovery=inherited['mapping_recovery'], preserved_pages=preserved,
                       registry_revision=registry_revision, mapping_overlay_entry=overlay_entry)
    if dry_run:
        return {**control, 'model_calls': 0}
    save(directory / 'control.json', control)
    paths = [*root.glob('metrics-*.json'), *root.glob('reentries/*/metrics-*.json')]
    metrics = sorted((_load_json(path) for path in paths), key=lambda row: row['started_at'])
    return run_stages(root, operation, sources, seeds, groups, metrics, entry=entry,
                      directory=directory, previous=previous, page_contract=page_contract,
                      fragment_repair=fragment_repair, fragment_exclusions=fragment_exclusions,
                      mapping_overlay=overlay, preserved_pages=preserved, registry_revision=registry_revision,
                      initial_holds=holds, split_output=split_output, replay_only=replay_only,
                      content_recovery=content_recovery)


def run_stages(root, operation, sources, seeds, groups, metrics, *, resumed=False,
               entry=None, directory=None, previous=None, page_contract=None, fragment_repair=None,
               fragment_exclusions=None, mapping_overlay=None, preserved_pages=(), registry_revision=None,
               initial_holds=None, split_output=None, replay_only=False, content_recovery=False):
    kb, kc, _ = runtime()
    output = directory or root
    board = operation['board']
    holds = dict(initial_holds or {})
    previous = previous or {}
    recovery_path = output / 'content-recovery.json'
    recovery_enabled = content_recovery or (not entry and operation.get('card_layout', {}).get('content_outcome_recovery'))
    if recovery_path.is_file():
        fragment_repair = _load_json(recovery_path)
    while True:
        live(root, operation, resumed=resumed, entry=entry)
        planning_started = time.monotonic()
        plan = advance(sources, seeds, groups, root / 'receipts', LIMIT, native_holds=holds,
                       page_contract=page_contract, fragment_repair=fragment_repair,
                       fragment_exclusions=fragment_exclusions, mapping_contract=operation.get('mapping_contract'),
                       mapping_overlay=mapping_overlay, preserved_pages=preserved_pages,
                       writing_contract=operation.get('writing_contract'),
                       split_output=split_output if split_output is not None else operation.get('split_output'),
                       page_namespace=operation.get('page_namespace'), card_layout=operation.get('card_layout'))
        planning_seconds = time.monotonic() - planning_started
        rows = plan.get('requests', [])
        if not rows and plan['stage'] == 'offline_build' and recovery_enabled and not recovery_path.exists():
            require(fragment_repair is None, 'automatic content recovery cannot replace an existing repair contract')
            from short_content_recovery import plan_recovery
            fragment_repair = plan_recovery(root, plan, sources, sys.modules[__name__])
            save(recovery_path, fragment_repair)
            # Historical exhausted/unknown requests remain fenced; the new calls have distinct bound identities.
            continue
        if replay_only:
            require(all(row['phase'] == 'short-write' and previous.get(row['id']) for row in rows),
                    'writing replay cannot dispatch a new model request')
        consumed_unknown = [r['id'] for r in rows if r['id'] in previous and not previous[r['id']]]
        if consumed_unknown:
            holds.update({rid: {'request_id': rid, 'reason': 'prior reserved completion remains unknown'}
                          for rid in consumed_unknown})
            continue
        save(output / f"stage-{len(metrics) + 1:02}-{plan['stage']}.json", plan)
        if not rows:
            final = 'partial' if (plan.get('stage') != 'offline_build' or plan.get('held')
                or plan.get('coordination_issues') or plan.get('reduced', {}).get('unresolved')
                or any(check['state'] != 'structurally_valid' for check in plan.get('checks', []))) else 'completed'
            break
        phase = rows[0]['phase']
        capacity = operation['page_capacity'] if phase == 'short-write' else operation['pass_capacity']
        stage_start = time.time()
        results = stage(root, operation, rows, capacity, resumed=resumed, entry=entry, saved_attempts=previous)
        item = stage_metrics(root, rows, results, stage_start, time.time())
        item['prepare_seconds'] = planning_seconds
        item['native_replays'] = sum('replays' in row for row in results)
        metrics.append(item)
        save(output / f"metrics-{len(metrics):02}-{phase}.json", item)
        origin = operation['origin']
        _, verified = source_boundary(Path(operation['vault']), origin['workflow_id'], operation['pins'], registry_revision)
        require(verified == origin, 'source changed across native stage')
        for row in results:
            if row['state'] == 'held':
                holds[row['request_id']] = {'task_id': row['task_id'], 'request_id': row['request_id']}
        if results and all(r['state'] == 'held' for r in results) and not entry and not recovery_enabled:
            final = 'held'
            break
    with closing(kc.connect(board=board)) as conn:
        remaining = [task.id for task in kb.list_tasks(conn) if task.status in ('ready', 'running', 'todo', 'triage')]
    result = {'state': final, 'stages': metrics, 'last_stage': plan['stage'], 'origin': operation['origin'], 'board': board,
              'page_contract': page_contract, 'complete_pages': len(plan.get('assembled_sections', {})),
              'native_active_or_ready': remaining, 'mineru_calls': 0, 'independent_build_review_calls': 0,
              'build_checks': len(plan.get('checks', [])), 'publication_eligible': False,
              'build_check_seconds': planning_seconds if plan['stage'] == 'offline_build' else None,
              'execution_holds': holds, 'wall_seconds': time.time() - operation['started_at']}
    save(output / 'result.json', result)
    return result


def start(root, vault, workflow_id, pins, operation_id, *, resumed=False, node=None, entry=None, dry_run=False,
          page_contract=None, fragment_repair=None, fragment_exclusions=None, mapping_recovery=None,
          registry_revision=None, split_output=None, separate_page_identities=False, mapping_from=None,
          transport_profile='shared', mapping_fences=(), replay_only=False, content_recovery=False):
    from short_evidence_pass import MAP_CONTRACT

    require(not os.environ.get('HERMES_KANBAN_TASK') and not os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),
            'trusted operator must start the experiment')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', operation_id), 'invalid experiment operation ID')
    require(split_output is None or (node == 'writing' and re.fullmatch(r'[a-f0-9]{64}', split_output)),
            'an explicit output split requires the exact writing fragment hash')
    require(not root.is_relative_to(vault), 'write experiment evidence outside the source Vault')
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / 'controller.lock', timeout=0, marker=False):
        require(registry_revision is None or mapping_recovery is not None,
                'historical source envelope requires exact mapping recovery')
        active = None
        inherited = None
        if node:
            operation_sha = sha(root / 'operation.json')
            controls = [_load_json(path) for path in (root / 'reentries').glob('*/control.json')]
            active = max((row for row in controls
                          if row['operation_sha256'] == operation_sha and row.get('page_contract')),
                         key=lambda row: row['started_at'], default=_load_json(root / 'operation.json'))
            if node == 'writing' and active.get('mapping_recovery') and mapping_recovery is None:
                inherited = active
                registry_revision = active['registry_revision']
        sources, origin = source_boundary(vault, workflow_id, pins, registry_revision)
        board = 'short-' + operation_id
        kb, kc, kd = runtime()
        from hermes_cli.profiles import profile_exists
        require(not profile_exists(ASSIGNEE), 'reserved program assignee is also a profile')
        cfg = configuration()
        operation = {'contract': CONTRACT, 'board': board, 'origin': origin, 'body_limit': LIMIT,
                     'vault': str(vault), 'pins': pins, 'started_at': time.time(),
                     'pass_capacity': cfg['pass_worker_concurrency'], 'page_capacity': cfg['reduce_page_concurrency']}
        seeds = seed_requests(sources)
        holds = None
        if resumed or node:
            operation = _load_json(root / 'operation.json')
            groups = mapping_groups(sources, operation.get('mapping_contract'))
            require(operation['board'] == board and operation['origin'] == origin and operation['pins'] == pins
                    and operation['body_limit'] == LIMIT, 'resume input identity changed')
            if node:
                active_contract = active.get('page_contract')
                selected = page_contract or active_contract
                if page_contract and page_contract != active_contract:
                    require(node == 'coordination', 'new page contract requires a coordination reentry')
                return reenter(root, operation, sources, seeds, groups, node, entry,
                               dry_run=dry_run, page_contract=selected,
                               fragment_repair=fragment_repair or active.get('fragment_repair'),
                               fragment_exclusions=fragment_exclusions or active.get('fragment_exclusions'),
                               mapping_recovery=mapping_recovery, registry_revision=registry_revision, inherited=inherited,
                               split_output=split_output or active.get('split_output') or operation.get('split_output'),
                               replay_only=replay_only, content_recovery=content_recovery)
            metrics = resume_mapping(root, operation, sources, seeds, groups)
        else:
            require(not (root / 'operation.json').exists(), 'operation already exists; inspect its durable evidence')
            require(not any(meta['slug'] == board for meta in kb.list_boards()), 'native test board already exists')
            page_contract = page_contract or FRAGMENT_CONTRACT
            operation['page_contract'] = page_contract
            operation['mapping_contract'] = MAP_CONTRACT
            operation['writing_contract'] = WRITE_CONTRACT
            if page_contract == FRAGMENT_CONTRACT:
                from short_cards import snapshot
                operation['card_layout'] = snapshot(vault)
            operation['split_output'] = True
            if mapping_from is not None:
                from model_transport import policy

                policy(transport_profile)
                operation['transport_profile'] = transport_profile
            if separate_page_identities:
                operation['page_namespace'] = operation_id
            groups = mapping_groups(sources, MAP_CONTRACT)
            if mapping_from is not None:
                holds = mapping_comparison(root, mapping_from, operation, sources, seeds, groups, mapping_fences)
            kb.create_board(board, name='Short evidence ' + operation_id, default_workdir=str(root))
            save(root / 'operation.json', operation)
            metrics = []
        return run_stages(root, operation, sources, seeds, groups, metrics, resumed=resumed,
                          page_contract=page_contract, initial_holds=holds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'resume-mapping', 'reenter', 'stop', 'worker', 'formal'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--origin-workflow')
    parser.add_argument('--sources', type=Path)
    parser.add_argument('--operation-id')
    parser.add_argument('--job', type=Path)
    parser.add_argument('--from-node', choices=tuple(NODE_ORDER))
    parser.add_argument('--reentry-id')
    parser.add_argument('--page-contract', choices=(FRAGMENT_CONTRACT,))
    parser.add_argument('--fragment-repair', type=Path)
    parser.add_argument('--fragment-exclusions', type=Path)
    parser.add_argument('--split-atomic-fragment')
    parser.add_argument('--mapping-recovery', type=Path)
    parser.add_argument('--source-registry-revision', type=int)
    parser.add_argument('--formal-authorization', type=Path)
    parser.add_argument('--existing-build')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = args.output.resolve()
    if args.action == 'worker':
        worker(root, args.job.resolve(strict=True))
    elif args.action == 'formal':
        from short_formal_build import execute
        if not (args.formal_authorization and args.reentry_id):
            parser.error('formal requires --formal-authorization and --reentry-id')
        result = execute(root, args.reentry_id, _load_json(args.formal_authorization),
                         args.dry_run, sys.modules[__name__], existing_build=args.existing_build)
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == 'stop':
        require(not os.environ.get('HERMES_KANBAN_TASK'), 'trusted operator required to stop')
        _load_json(root / 'operation.json')
        if args.reentry_id:
            require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', args.reentry_id), 'invalid reentry ID')
            directory = root / 'reentries' / args.reentry_id
            _load_json(directory / 'control.json')
            marker = directory / 'stop.json'
        else:
            marker = root / ('stop-resume.json' if (root / 'resume.json').exists() else 'stop.json')
        save(marker, {'operation': CONTRACT, 'reason': 'controller interrupted or stage failed'})
        print(json.dumps({'stop_requested': True, 'marker': str(marker)}))
    else:
        if args.action == 'reenter' and not (args.from_node and args.reentry_id):
            parser.error('reenter requires --from-node and --reentry-id')
        if (args.fragment_repair or args.fragment_exclusions) and (args.action != 'reenter' or args.from_node != 'writing'):
            parser.error('fragment repair and exclusions require writing reentry')
        if (args.mapping_recovery or args.source_registry_revision is not None) and (
                args.action != 'reenter' or args.from_node != 'mapping'):
            parser.error('mapping recovery requires mapping reentry')
        if args.action != 'reenter' and (args.from_node or args.reentry_id or args.dry_run):
            parser.error('node and dry-run arguments require reenter')
        result = start(root, args.vault.resolve(strict=True), args.origin_workflow, _load_json(args.sources),
                       args.operation_id, resumed=args.action == 'resume-mapping',
                       node=args.from_node, entry=args.reentry_id, dry_run=args.dry_run,
                       page_contract=args.page_contract,
                       fragment_repair=_load_json(args.fragment_repair) if args.fragment_repair else None,
                       split_output=args.split_atomic_fragment,
                       fragment_exclusions=_load_json(args.fragment_exclusions) if args.fragment_exclusions else None,
                       mapping_recovery=_load_json(args.mapping_recovery) if args.mapping_recovery else None,
                       registry_revision=args.source_registry_revision)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
