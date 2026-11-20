"""Reconcile exact native usage and explicitly close a drained incremental run."""
from collections import Counter
from contextlib import closing
from pathlib import Path, PurePosixPath
import json
import re
import sqlite3

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, pinned, read_pin, safe_id
from hermes_source_units.source_units import _exclusive_lock, _load_json, _sha, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from incremental_native import mutate
from short_evidence_pass import require

CONTRACT = 'incremental-closure-accounting/v1'
REQUEST = {'run_id', 'actor', 'operation_id', 'expected_revision', 'run_revision', 'boundary_fingerprint'}
HEX = re.compile(r'[a-f0-9]{64}')


def pin(vault, path):
    return pinned(vault, path.relative_to(vault).as_posix())


def session_row(connection, session_id):
    row = connection.execute('SELECT source, ended_at, api_call_count, input_tokens, output_tokens '
                             'FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not row:
        return None
    source, ended, calls, input_tokens, output_tokens = row
    return {'source': source, 'ended_at_us': round(ended * 1_000_000) if ended is not None else None,
            'calls': calls, 'input_tokens': input_tokens, 'output_tokens': output_tokens}


def returned_usage(native, original, job, saved, audit, ended, connection):
    from fixed_semantic_recovery import transcript_reply

    model = audit.get('model', {})
    call = saved['request']
    require(call['id'] == original['id'] or native.checked_retry(original, call, job, audit),
            'closure audit is not the original request or its permitted correction')
    require(model.get('parent_native_run_id') == str(ended.id), 'native usage belongs to a different execution')
    wire = native.checked_projection(call)['wire']
    proof = {'model': model, 'response_sha256': fingerprint(audit['raw']), 'user_sha256': fingerprint(wire['user']),
             'image_inputs': [{k: v for k, v in image.items() if k != 'url'} for image in call['image_inputs']]}
    if transcript_reply(proof, wire) != audit['raw']:
        return None
    sid = model['session_id']
    row = session_row(connection, sid)
    if (not row or row['source'] != 'kanban' or not row['ended_at_us'] or row['calls'] != 1
            or any(type(row[k]) is not int or row[k] < 0 for k in ('input_tokens', 'output_tokens'))):
        return None
    return {**row, 'session_id': sid, 'response_sha256': proof['response_sha256'],
            'request_sha256': fingerprint(wire)}


def failure_evidence(vault, workspace, native, audit, saved, entry, connection):
    from hermes_constants import get_hermes_home
    from model_transport import CONTRACT as HTTP_CONTRACT

    _, separator, payload = audit.get('error', '').partition(' at $: ')
    if not separator or not payload.startswith('{'):
        return
    failure = json.loads(payload)
    path = Path(failure.get('report', '')).resolve()
    if path.parent != workspace / 'call-failures' or not path.is_file():
        return
    evidence = _load_json(path)
    grant = saved['grant']
    wire = native.checked_projection(saved['request'])['wire']
    require(path.name == fingerprint(evidence) + '.json' and evidence['task_id'] == grant['task_id']
            and str(evidence['run_id']) == str(grant['run_id'])
            and evidence['session_id'] == failure['session_id'] and evidence['wire_sha256'] == fingerprint(wire),
            'native failure record is not bound to the original attempt')
    entry['failure'] = pin(vault, path)
    entry['failure_type'] = {**{key: evidence.get(key) for key in ('state', 'source')},
                             'class': evidence['error'].get('class'), 'http_status': evidence['error'].get('status_code')}
    session = session_row(connection, evidence['session_id'])
    if session:
        entry['observed_session'] = {**session, 'session_id': evidence['session_id']}
    transport = evidence.get('http_transport') or {}
    if transport.get('contract') != HTTP_CONTRACT:
        return
    key = fingerprint([str(workspace), grant,
                       {'ref': Path(entry['attempt']['ref']).relative_to(workspace.relative_to(vault)).as_posix(),
                        'sha256': entry['attempt']['sha256'], 'ordinal': saved['ordinal'],
                        'request_sha256': saved['request']['id']}, evidence['wire_sha256']])
    sends = []
    for pointer in transport['sends']:
        target = _vault_path(vault, workspace.relative_to(vault).as_posix() + '/' + pointer['ref'])
        require(target.parent == workspace / 'http-sends' and target.name.startswith(key + '-')
                and _sha(target.read_bytes()) == pointer['sha256'], 'native HTTP send does not match its exact reservation')
        sends.append(pin(vault, target))
    entry['http_sends'] = sends
    db = get_hermes_home() / 'fixed-model-http.sqlite3'
    if not db.is_file():
        return
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as journal:
        count, = journal.execute('SELECT count(*) FROM sends WHERE id=?', (key,)).fetchone()
    entry['http_journal'] = {'request_key': key, 'send_count': count}
    actual = dict.fromkeys(('calls', 'input_tokens', 'output_tokens'), 0)
    if (evidence.get('state') == 'request_rejected' and evidence.get('source') == 'local_request_guard'
            and transport['physical_attempts'] == count == 0 and not sends
            and session and session['source'] == 'kanban' and session['ended_at_us']
            and all(type(session[k]) is int and session[k] == n for k, n in actual.items())):
        entry.update(disposition='settled_zero', actual=actual, session=entry['observed_session'],
                     reason='bound_local_rejection_without_http_send')


def inspect_reservations(domain, run):
    import run_short_evidence_test as native
    from hermes_state import SessionDB

    vault, root = domain.vault, domain.root(run['run_id'])
    reserved = {row['request_fingerprint']: (key, row) for key, row in run['reservations'].items()
                if row['state'] == 'reserved'}
    if not reserved:
        return {}
    entries, sessions = {}, set()
    kb, _, _ = native.runtime()
    with closing(SessionDB(read_only=True)) as state, closing(sqlite3.connect(
            Path(state.db_path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        for op in sorted((root / 'native').rglob('operation.json')):
            workspace = op.parent
            matches = []
            for job in sorted((workspace / 'jobs').glob('*.json')):
                if not HEX.fullmatch(job.stem):
                    continue
                for ordinal in (1, 2):
                    ident = fingerprint({'request': job.stem, 'ordinal': ordinal})
                    if ident in reserved:
                        matches.append((job, ordinal, *reserved[ident]))
            if not matches:
                continue
            operation = _load_json(op)
            scope = operation['incremental']
            require(workspace.resolve() == workspace and operation['workflow_mode'] == 'incremental_v1'
                    and operation['actor'] == run['actor'] and Path(operation['vault']).resolve() == vault
                    and scope['run_id'] == run['run_id'], 'closure usage left the original native scope')
            board = operation['board']
            with closing(completion_connection(board)) as tasks:
                by_body = {}
                for task in kb.list_tasks(tasks):
                    by_body.setdefault(task.body, []).append(task)
                for job, ordinal, key, reservation in matches:
                    row = _load_json(job)['request']
                    require(row['id'] == job.stem and key not in entries and reservation['phase'] == scope['stage'],
                            'closure request or reservation is duplicated or changed')
                    entry = {'reservation': reservation, 'job': pin(vault, job), 'ordinal': ordinal,
                             'disposition': 'retained_unconfirmed', 'actual': None}
                    entries[key] = entry
                    attempt = job.with_name(job.stem + f'.attempt-{ordinal}.json')
                    audit_path = job.with_name(job.stem + f'.audit-{ordinal}.json')
                    result = job.with_suffix('.result.json')
                    if result.is_file():
                        entry['native_result'] = pin(vault, result)
                    if not attempt.is_file():
                        require(not audit_path.exists(), 'closure audit has no bound attempt')
                        entry['reason'] = 'reservation_without_attempt_evidence'
                        continue
                    saved = _load_json(attempt)
                    grant = saved['grant']
                    found = by_body.get(native.task_body(workspace, job), [])
                    require(len(found) == 1, 'closure attempt lacks its exact original task')
                    task, = found
                    require(saved['ordinal'] == ordinal and task.created_by == operation['contract']
                            and not task.claim_lock and grant['board'] == board and grant['task_id'] == task.id,
                            'closure attempt lacks an ended native binding')
                    ended = next((r for r in kb.list_runs(tasks, task.id, include_active=False)
                                  if r.id == grant['run_id']), None)
                    require(ended and ended.ended_at, 'closure attempt still has an active native owner')
                    entry.update(attempt=pin(vault, attempt),
                                 native={'board': board, 'task_id': task.id, 'run_id': ended.id,
                                         'outcome': ended.outcome, 'ended_at_us': round(ended.ended_at * 1_000_000)})
                    if not audit_path.is_file():
                        entry['reason'] = 'ended_attempt_without_audit'
                        continue
                    audit = _load_json(audit_path)
                    require(audit['grant'] == grant and audit['ordinal'] == ordinal
                            and audit['request_id'] == saved['request']['id'], 'closure audit identity changed')
                    entry.update(audit=pin(vault, audit_path), reason=audit['state'])
                    if audit['state'] != 'returned':
                        failure_evidence(vault, workspace, native, audit, saved, entry, connection)
                        continue
                    session = returned_usage(native, row, job, saved, audit, ended, connection)
                    if not session:
                        entry['reason'] = 'returned_usage_unverified'
                        continue
                    sid = session['session_id']
                    require(sid not in sessions, 'one native session cannot settle two reservations')
                    sessions.add(sid)
                    actual = {k: session[k] for k in ('calls', 'input_tokens', 'output_tokens')}
                    if any(n > reservation['max_' + k] for k, n in actual.items()):
                        entry.update(reason='recorded_usage_exceeds_reservation', observed_session=session)
                        continue
                    entry.update(disposition='settled_actual', actual=actual, session=session,
                                 reason='bound_native_response_and_session_usage')
    require(entries.keys() == {key for key, _ in reserved.values()}, 'closure lost a historical reservation or exact native job')
    return entries


def current_release(domain, delivery):
    from hermes_source_units.incremental_delivery import readiness

    if not delivery:
        return None, None
    value = read_pin(domain.vault, delivery)
    service = FileVaultFinalizeService(domain.vault)
    release_id = value['release_id']
    path = service._manifest_path(release_id)
    provider = value['provider']
    report = _load_json(_vault_path(domain.vault, '_system/reports/retrieval-index-manifest.json'))
    fields = ('release_id', 'release_hash', 'index_generation', 'status')
    require(value['state'] == 'committed' and service.status()['current_release_id'] == release_id
            and value['release_hash'] == _sha(path.read_bytes()) and provider['release_id'] == release_id
            and provider['release_hash'] == value['release_hash'] and provider['status'] == 'ready'
            and provider['index_generation'] and all(report[k] == provider[k] for k in fields)
            and readiness(domain.vault)['available'], 'closure release and Provider generation differ')
    service.validate(release_id)
    return service, _load_json(path)


def directory_navigation(domain, delivery):
    from short_cards import write_navigation
    from short_navigation import OPTIONAL_ERRORS

    service, release = current_release(domain, delivery)
    if not release:
        return []
    eligible = {row['id'] for row in release['index_eligibility'] if row['kind'] == 'knowledge_page' and row['eligible']}
    directories = {str(PurePosixPath(row['current_path']).parent) for row in release['subjects'] if row['page_id'] in eligible}
    try:
        return [{k: v for k, v in row.items() if k != 'changed'} for row in
                write_navigation(service, release, directories, typed=True, guarded=True)]
    except OPTIONAL_ERRORS as error:
        return [{'state': 'deferred', 'reason': str(error)}]


def finish(domain, run, native_drained, delivery):
    run_id = run['run_id']
    pointer = IncrementalControl(domain.vault, run_id).status().get('partial_closure')
    fields = {'native_drained': native_drained, 'final_delivery': delivery}
    if pointer:
        report = read_pin(domain.vault, pointer)
        require(report['run_revision'] == run['revision'] and report['final_delivery'] == delivery
                and report['native_drained'] == native_drained
                and report['entries'] == inspect_reservations(domain, run), 'native accounting changed since closure authorization')
        fields['usage_reconciliation'] = pointer
    elif any(row['state'] == 'reserved' for row in run['reservations'].values()):
        return mutate(domain, run_id, 'close_run', 'close-' + run_id, **fields)
    fields['directory_navigation'] = directory_navigation(domain, delivery)
    return mutate(domain, run_id, 'close_run', 'close-' + run_id, **fields)


def recover(domain, request, *, preview=False):
    from incremental_ingest_controller import closure_inputs

    require(set(request) == REQUEST | (set() if preview else {'expected_report_sha256'}),
            'partial closure requires one exact operator intent')
    run_id, op = safe_id(request['run_id']), safe_id(request['operation_id'])
    root = domain.root(run_id)
    path = root / 'closure-recoveries' / (op + '.json')
    control = IncrementalControl(domain.vault, run_id)
    with _exclusive_lock(root / '.controller.lock', timeout=0, marker=False):
        head, run = control.status(), domain.status(run_id)
        require(request['actor'] == run['actor'] == head['actor'], 'partial closure actor changed')
        if not preview and path.is_file():
            report = _load_json(path)
        else:
            boundary = head['boundary']
            require(head['state'] == 'paused' and not head['active_scopes'] and boundary['stage'] == 'closure'
                    and boundary['scope'] == {'scope_kind': 'run', 'scope_id': run_id}
                    and run['active_round_id'] is None and run['state'] == 'active'
                    and request['run_revision'] == run['revision'] and request['expected_revision'] == head['revision']
                    and request['boundary_fingerprint'] == fingerprint(boundary),
                    'partial closure needs the exact drained closure checkpoint')
            native, delivery = closure_inputs(domain, run)
            current_release(domain, delivery)
            report = {'contract': CONTRACT, **{k: request[k] for k in REQUEST - {'expected_revision'}},
                      'control_revision': head['revision'], 'native_drained': native,
                      'final_delivery': delivery, 'entries': inspect_reservations(domain, run)}
        digest = fingerprint(report)
        require(all(report[k] == request[k] for k in REQUEST - {'expected_revision'})
                and report['control_revision'] == request['expected_revision'], 'partial closure intent differs')
        summary = {'report_sha256': digest, 'counts': dict(Counter(e['disposition'] for e in report['entries'].values())),
                   'reservations': {key: {k: row[k] for k in ('disposition', 'reason', 'actual')}
                                    for key, row in report['entries'].items()}}
        if preview:
            return summary
        require(digest == request['expected_report_sha256'], 'partial closure preview digest changed')
        pointer = domain._immutable(path, report)
        if run['state'] != 'active':
            require(head.get('partial_closure') == pointer, 'terminal closure has a different authorization')
            return {**summary, 'closure': read_pin(domain.vault, run['closure']), 'closure_ref': run['closure']}
        control.control({'run_id': run_id, 'actor': run['actor'], 'operation_id': op, 'action': 'continue',
                         'expected_revision': request['expected_revision'], 'boundary_fingerprint': request['boundary_fingerprint'],
                         'partial_closure': pointer})
        require(control.status().get('partial_closure') == pointer, 'partial closure authorization was not retained')
        native, delivery = closure_inputs(domain, run)
        closure = finish(domain, run, native, delivery)
        return {**summary, 'closure': closure, 'closure_ref': domain.status(run_id)['closure']}
