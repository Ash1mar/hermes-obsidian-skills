"""Independent source sealing, finite rounds and durable cumulative consumption."""
from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import ContractError
from hermes_source_units.incremental_ingest import CONTRACTS, FileIncrementalIngestService
from hermes_source_units.incremental_sources import pinned
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


@pytest.fixture
def incremental(vault):
    refs = publish_sources(vault, ['# A\nFirst independent source.\n', '# B\nSecond independent source.\n'])
    admission = []
    native = []

    def admit(scope):
        admission.append(scope)
        return {'allowed': True, 'input_fingerprint': scope['input_fingerprint'], 'control_epoch': 1}

    def completion(_vault, proof, expected):
        assert proof == {'board': 'isolated', 'task_id': 'task-1', 'run_id': 1}
        native.append(expected)
        return proof

    service = FileIncrementalIngestService(vault, check_admission=admit, native_evidence=completion)
    sources = [{'path': f'10_Raw/source-{i}.md',
                'sha256': hashlib.sha256((vault / f'10_Raw/source-{i}.md').read_bytes()).hexdigest()}
               for i in (1, 2)]
    request = {'run_id': 'fixture', 'operation_id': 'start-fixture', 'actor': 'builder',
               'workflow_mode': 'incremental_v1', 'source_workflow_id': 'ingest-fixture', 'sources': sources,
               'contracts': CONTRACTS, 'control_ref': '_system/control.json', 'policy': {'max_maps_per_round': 1},
               'budget': {'max_calls': 3, 'max_input_tokens': 1000, 'max_output_tokens': 300,
                          'phase_limits': {'mapping': 2, 'writing': 1}}}
    service.start(request)
    outcomes = [{'path': source['path'], 'content_sha256': source['sha256'], 'status': 'ready',
                 'resource_id': ref['unit_ref']['resource_id'], 'unit_set_id': ref['unit_ref']['unit_set_id'],
                 'artifact_refs': []} for source, ref in zip(sources, refs)]
    workflow = vault / '_system/ledgers/ingest-workflows/ingest-fixture.json'
    put(workflow, {'workflow_id': 'ingest-fixture', 'actor': 'builder', 'cancel_requested': False,
                   'revision': 3, 'state': 'source_preparing', 'source_outcomes': outcomes[:1]})
    return service, request, outcomes, workflow, refs, admission, native


def mutation(service, operation, **values):
    return {'run_id': 'fixture', 'actor': 'builder', 'operation_id': operation,
            'expected_revision': service.status('fixture')['revision'], **values}


def seal(incremental, index=0):
    service, _, outcomes, _, _, _, _ = incremental
    request = mutation(service, f'seal-{index}', source_path=outcomes[index]['path'], outcome=outcomes[index],
                       native_completion={'board': 'isolated', 'task_id': 'task-1', 'run_id': 1})
    return service.seal_source_snapshot(request)


def test_mapping_hold_preserves_seal_and_pins_exhausted_wait(incremental):
    from hermes_source_units.incremental_qualification import WAIT_CONTRACT, qualification_id

    service, _, outcomes, _, _, _, _ = incremental
    snapshot = seal(incremental)['snapshot']
    sid, source = snapshot['snapshot_id'], outcomes[0]['path']
    old = service.status('fixture')['sources'][source]
    path = service.root('fixture') / 'source-qualification' / sid / 'wait.json'
    evidence = {'contract': WAIT_CONTRACT, 'run_id': 'fixture', 'snapshot_id': sid,
                'qualification_id': qualification_id('fixture', sid), 'retry_at': None, 'state': 'exhausted',
                'attempts': [{'attempt': i, 'lock': '_system/ledgers/ingest-workflows/.dispatch-locks/ingest-fixture.lock',
                              'error_code': 'LOCK_TIMEOUT', 'wait_ms': 5000} for i in (1, 2, 3)]}
    for invalid in ({**evidence, 'attempts': evidence['attempts'][:2]}, {**evidence, 'snapshot_id': 'another'}):
        put(path, invalid)
        request = mutation(service, 'hold-a', source_path=source, snapshot_id=sid,
                           wait=pinned(service.vault, path.relative_to(service.vault).as_posix()))
        with pytest.raises(ContractError, match='not evidenced as exhausted'):
            service.hold_source_mapping(request)
    put(path, evidence)
    request = mutation(service, 'hold-a', source_path=source, snapshot_id=sid,
                       wait=pinned(service.vault, path.relative_to(service.vault).as_posix()))
    hold = service.hold_source_mapping(request)
    current = service.status('fixture')
    assert current['sources'][source] == {**old, 'state': 'mapping_held', 'mapping_hold': hold}
    assert current['reservations'] == {} and current['map_result_ids'] == []


def map_source(incremental, source):
    service, _, _, _, _, _, _ = incremental
    snap = source['snapshot']
    fp = fingerprint({'snapshot': snap['snapshot_id'], 'phase': 'mapping'})
    reserved = service.reserve_budget(mutation(service, 'reserve-' + snap['snapshot_id'][-8:], reservation={
        'request_fingerprint': fp, 'phase': 'mapping', 'max_calls': 1,
        'max_input_tokens': 100, 'max_output_tokens': 40}))
    raw = service.reports('fixture') / 'fixture-native.json'
    put(raw, {'source': snap['snapshot_id'], 'native': 'fixture'})
    req = mutation(service, 'map-' + snap['snapshot_id'][-8:], snapshot_id=snap['snapshot_id'],
                   request_fingerprint=fp, budget_id=reserved['budget_id'],
                   native_completion={'board': 'isolated', 'task_id': 'task-1', 'run_id': 1}, task_body={'job': 'fixture'},
                   alias_table={f'S1-B{i}': {'snapshot_id': snap['snapshot_id'], **unit}
                                for i, unit in enumerate(snap['units'], 1)},
                   coverage=[{'unit_ref': unit['unit_ref'], 'disposition': 'mapped'} for unit in snap['units']],
                   results=[pinned(service.vault, raw.relative_to(service.vault).as_posix())])
    return service.commit_source_map(req)


@pytest.mark.parametrize('phase,reason', [('short-topics', 'seed-incomplete'), ('short-map', 'required-map-incomplete')])
def test_failed_native_map_scope_is_held_without_a_phantom_map_or_blocking_other_source(incremental, monkeypatch,
                                                                                       phase, reason):
    from hermes_source_units import incremental_sources

    service, _, outcomes, workflow, _, _, _ = incremental
    snapshot = seal(incremental)['snapshot']
    sid, source = snapshot['snapshot_id'], outcomes[0]['path']
    workspace = service.root('fixture') / 'native/source' / sid / 'mapping'
    operation_path, job = workspace / 'operation.json', workspace / 'jobs/job-1.json'
    put(operation_path, {'contract': 'test-native', 'board': 'isolated-map', 'actor': 'builder',
                         'vault': str(service.vault), 'workflow_mode': 'incremental_v1',
                         'incremental': {'run_id': 'fixture', 'scope_kind': 'source', 'scope_id': sid,
                                         'stage': 'mapping', 'snapshots': [sid]}})
    put(job, {'request': {'id': 'job-1', 'phase': phase, 'view': {'assigned': ['S1-B1']}}})
    job_pin = pinned(service.vault, job.relative_to(service.vault).as_posix())
    task = SimpleNamespace(id='task-2', status='blocked', claim_lock='live', created_by='test-native',
                           body=json.dumps({'contract': 'test-native', 'operation_sha256': hashlib.sha256(
                               operation_path.read_bytes()).hexdigest(), 'job': job.name, 'job_sha256': job_pin['sha256']}))
    ended = SimpleNamespace(id=2, ended_at=10, outcome='gave_up')
    kb = SimpleNamespace(get_task=lambda *_: task, list_runs=lambda *_, **__: [ended])
    monkeypatch.setitem(sys.modules, 'hermes_cli', SimpleNamespace(kanban_db=kb))
    monkeypatch.setattr(incremental_sources, 'completion_connection', lambda _: SimpleNamespace(close=lambda: None))
    evidence_path = workspace / 'execution-hold.json'
    put(evidence_path, {'contract': incremental_sources.MAP_ISOLATION, 'run_id': 'fixture', 'snapshot_id': sid,
                        'board': 'isolated-map', 'reason': reason,
                        'jobs': [{'job': job_pin, 'task_id': task.id, 'run_id': ended.id, 'state': 'unconfirmed'}]})
    evidence = pinned(service.vault, evidence_path.relative_to(service.vault).as_posix())
    request = mutation(service, 'hold-a', source_path=source, snapshot_id=sid, execution_hold=evidence)
    with pytest.raises(ContractError, match='task binding changed'):
        service.hold_source_mapping(request)
    task.claim_lock = None
    hold = service.hold_source_mapping(request)
    assert hold['execution_hold'] == evidence and service.hold_source_mapping(request) == hold
    run = service.status('fixture')
    assert run['sources'][source]['state'] == 'mapping_held' and run['sources'][source]['snapshot_id'] == sid
    assert run['map_result_ids'] == [] and run['reservations'] == {}

    put(workflow, {**json.loads(workflow.read_text()), 'source_outcomes': outcomes})
    source_b = seal(incremental, index=1)
    mapped = map_source(incremental, source_b)
    round_input = service.freeze_round(mutation(service, 'freeze-b', round_id='round-1', max_maps=1))
    assert round_input['map_result_ids'] == [mapped['map_result_id']]
    assert round_input['snapshots'] == [source_b['snapshot']['snapshot_id']]


def test_source_a_maps_while_b_is_preparing_and_b_cannot_stale_a(incremental):
    service, _, outcomes, workflow, _, _, native = incremental
    source_a = seal(incremental)
    mapped = map_source(incremental, source_a)
    before = (service.reports('fixture') / 'source-snapshots' / (source_a['snapshot']['snapshot_id'] + '.json')).read_bytes()
    body = json.loads(workflow.read_text())
    body.update(revision=99, state='planning', source_outcomes=outcomes)
    put(workflow, body)
    assert service.validate_source_snapshot('fixture', source_a['snapshot']['snapshot_id']) == source_a['snapshot']
    assert (service.reports('fixture') / 'source-snapshots' / (source_a['snapshot']['snapshot_id'] + '.json')).read_bytes() == before
    assert service.status('fixture')['sources'][outcomes[1]['path']]['state'] == 'pending'
    round_input = service.freeze_round(mutation(service, 'freeze-1', round_id='round-1', max_maps=1))
    assert round_input['map_result_ids'] == [mapped['map_result_id']]
    assert round_input['snapshots'] == [source_a['snapshot']['snapshot_id']]
    assert native[0]['source_workflow_id'] == 'ingest-fixture'


def test_changed_source_and_revoked_governance_block_snapshot_reuse(incremental):
    service, _, _, _, _, _, _ = incremental
    snap = seal(incremental)['snapshot']
    original = service.vault / snap['original']['ref']
    prior = original.read_bytes()
    original.write_bytes(b'changed')
    with pytest.raises(ContractError, match='original source changed'):
        service.validate_source_snapshot('fixture', snap['snapshot_id'])
    original.write_bytes(prior)
    registry = service.vault / '_system/metadata/document-registry.json'
    body = json.loads(registry.read_text())
    body['records'][0]['governance_status'] = 'withdrawn'
    put(registry, body)
    with pytest.raises(ContractError, match='governance no longer permits'):
        service.validate_source_snapshot('fixture', snap['snapshot_id'])


def test_partial_closure_settles_only_the_authorized_usage(incremental, monkeypatch):
    from hermes_source_units import incremental_ingest

    service, _, _, workflow, _, _, _ = incremental
    base = service.root('fixture')
    run = service.status('fixture')
    run['sources'] = {key: {'state': 'failed'} for key in run['sources']}
    known = {'request_fingerprint': 'known', 'phase': 'mapping', 'state': 'reserved', 'actual': None,
             'max_calls': 1, 'max_input_tokens': 100, 'max_output_tokens': 50}
    unknown = {**known, 'request_fingerprint': 'unknown'}
    run['reservations'] = {'known': known, 'unknown': unknown}
    put(base/'run.json', run)
    put(workflow, {**json.loads(workflow.read_text()), 'kanban': {'board_id': 'fixture'}})
    monkeypatch.setattr(incremental_ingest, 'verify_native_drain', lambda *_: {'boards': ['fixture']})
    native = {'source_workflow_id': run['source_workflow_id'], 'boards': ['fixture']}
    with pytest.raises(ContractError, match='remains unsettled'):
        service.close_run(mutation(service, 'strict-close', native_drained=native))
    evidence = base/'native-audit.json'
    put(evidence, {'state': 'returned'})
    audit = pinned(service.vault, evidence.relative_to(service.vault).as_posix())
    actual = {'calls': 1, 'input_tokens': 31, 'output_tokens': 7}
    manifest = base/'closure-recoveries/fixture-proof.json'
    report = {'contract': 'incremental-closure-accounting/v1', 'run_id': 'fixture', 'actor': 'builder',
              'run_revision': run['revision'], 'entries': {
                  'known': {'reservation': known, 'disposition': 'settled_actual', 'actual': actual,
                            'audit': audit, 'session': {'session_id': 'native-1'}},
                  'unknown': {'reservation': unknown, 'actual': None}}}
    put(manifest, report)
    pointer = pinned(service.vault, manifest.relative_to(service.vault).as_posix())
    head = base/'control/head.json'
    put(head, {'state': 'active'})
    closure = mutation(service, 'authorized-close', native_drained=native, usage_reconciliation=pointer)
    with pytest.raises(ContractError, match='authorized closure checkpoint'):
        service.close_run(closure)
    put(head, {'state': 'active', 'partial_closure': pointer})
    result = service.close_run(closure)
    assert service.close_run(closure) == result
    assert result['state'] == 'partial' and result['retained_reservation_ids'] == ['unknown']
    assert result['usage']['unknown'] == unknown and result['usage']['known']['actual'] == actual


def test_closure_usage_session_serializes_native_fractional_time(monkeypatch):
    library = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator' / 'lib'
    monkeypatch.syspath_prepend(str(library))
    from incremental_closure import session_row

    sid = '20261124_131500_d3dc9f'
    records = {sid: {'source': 'kanban', 'ended_at': 12.625, 'api_call_count': 1,
                     'input_tokens': 0, 'output_tokens': 4}}
    row = session_row(records.get, sid)
    assert row == {'source': 'kanban', 'ended_at_us': 12_625_000, 'calls': 1,
                   'input_tokens': 0, 'output_tokens': 4}
    assert fingerprint(row)
    assert session_row(records.get, sid[:-3]) is None
    del records[sid]['input_tokens']
    assert session_row(records.get, sid)['input_tokens'] is None


@pytest.mark.parametrize('recorded_send,missing_session', [(False, False), (True, False), (False, True)])
def test_closure_local_zero_requires_exact_session_and_zero_sends(tmp_path, monkeypatch, recorded_send,
                                                             missing_session):
    from contextlib import closing
    import sqlite3
    from incremental_closure import failure_evidence, pin
    from model_transport import CONTRACT

    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: home))
    workspace = tmp_path / 'native' / 'mapping'
    sid = '20261124_131500_d3dc9f'
    grant = {'board': 'board', 'task_id': 'task', 'run_id': 9}
    wire = {'user': 'original request'}
    saved = {'ordinal': 1, 'grant': grant, 'request': {'id': 'request'}}
    attempt = workspace / 'jobs' / 'request.attempt-1.json'
    put(attempt, saved)
    evidence = {'task_id': grant['task_id'], 'run_id': str(grant['run_id']), 'session_id': sid,
                'wire_sha256': fingerprint(wire), 'state': 'request_rejected', 'source': 'local_request_guard',
                'error': {'class': 'Rejected'},
                'http_transport': {'contract': CONTRACT, 'physical_attempts': 0, 'sends': []}}
    path = workspace / 'call-failures' / (fingerprint(evidence) + '.json')
    put(path, evidence)
    entry = {'attempt': pin(tmp_path, attempt), 'disposition': 'retained_unconfirmed', 'actual': None}
    request_key = fingerprint([str(workspace), grant, {
        'ref': 'jobs/request.attempt-1.json', 'sha256': entry['attempt']['sha256'], 'ordinal': 1,
        'request_sha256': saved['request']['id'],
    }, evidence['wire_sha256']])
    with closing(sqlite3.connect(home / 'fixed-model-http.sqlite3')) as journal, journal:
        journal.execute('CREATE TABLE sends (id TEXT)')
        if recorded_send:
            journal.execute('INSERT INTO sends VALUES (?)', (request_key,))
    native = SimpleNamespace(checked_projection=lambda _: {'wire': wire})
    audit = {'error': 'NATIVE_REQUEST_REJECTED at $: ' + json.dumps({'report': str(path), 'session_id': sid})}
    seen = []

    def lookup(full_id):
        seen.append(full_id)
        return None if missing_session else {'source': 'kanban', 'ended_at': 12.5, 'api_call_count': 0,
                                             'input_tokens': 0, 'output_tokens': 0}

    failure_evidence(tmp_path, workspace, native, audit, saved, entry, lookup)
    assert seen == [sid] and entry['http_journal'] == {'request_key': request_key, 'send_count': int(recorded_send)}
    if recorded_send or missing_session:
        assert entry['disposition'] == 'retained_unconfirmed' and entry['actual'] is None
    else:
        assert entry['disposition'] == 'settled_zero'
        assert entry['actual'] == {'calls': 0, 'input_tokens': 0, 'output_tokens': 0}


@pytest.mark.parametrize('historical', [False, True])
def test_accounting_hold_without_configured_pause_and_idempotent_confirm(incremental, monkeypatch, historical):
    from hermes_source_units import incremental_ingest
    from hermes_source_units.incremental_control import IncrementalControl
    import incremental_ingest_controller as controller
    import incremental_closure as closure

    service, _, _, workflow, _, _, _ = incremental
    base = service.root('fixture')
    run = service.status('fixture')
    run['sources'] = {key: {'state': 'failed'} for key in run['sources']}
    row = {'request_fingerprint': 'unknown', 'phase': 'mapping', 'state': 'reserved', 'actual': None,
           'max_calls': 1, 'max_input_tokens': 100, 'max_output_tokens': 50}
    run['reservations'] = {'unknown': row}
    put(base/'run.json', run)
    put(workflow, {**json.loads(workflow.read_text()), 'kanban': {'board_id': 'fixture'}})
    native = {'source_workflow_id': run['source_workflow_id'], 'boards': ['fixture']}
    control = IncrementalControl(service.vault, 'fixture')
    control.initialize('builder', [])
    monkeypatch.setattr(controller, 'closure_inputs', lambda *_: (native, None))
    monkeypatch.setattr(incremental_ingest, 'verify_native_drain', lambda *_: native)
    evidence = {'unknown': {'reservation': row, 'disposition': 'retained_unconfirmed',
                            'reason': 'completion_unknown', 'actual': None}}
    monkeypatch.setattr(closure, 'inspect_reservations', lambda *_: evidence)
    request = {'run_id': 'fixture', 'actor': 'builder', 'operation_id': 'review',
               'expected_revision': 1, 'run_revision': run['revision'], 'boundary_fingerprint': None}
    if historical:
        with pytest.raises(ContractError, match='remains unsettled'):
            closure.mutate(service, 'fixture', 'close_run', 'close-fixture', native_drained=native, final_delivery=None)
        old_intent = (base/'runtime-requests/close-fixture.json').read_bytes()
    else:
        controller.close(service, 'fixture')
        head = control.status()
        request.update(expected_revision=head['revision'], boundary_fingerprint=fingerprint(head['boundary']))
    review = closure.recover(service, request, preview=True)
    head = control.status()
    assert head['state'] == 'paused' and head['pause_after'] == []
    assert head['boundary']['evidence']['reservation_ids'] == ['unknown']
    assert not (service.reports('fixture')/'closure.json').exists()
    assert closure.recover(service, review['request'], preview=True) == review
    with pytest.raises(ContractError, match='requires recover-closure'):
        control.control({'action': 'continue', 'run_id': 'fixture', 'actor': 'builder', 'operation_id': 'plain',
                         'expected_revision': head['revision'], 'boundary_fingerprint': fingerprint(head['boundary'])})
    request = {**review['request'], 'expected_report_sha256': review['report_sha256']}
    with pytest.raises(ValueError, match='preview digest changed'):
        closure.recover(service, {**request, 'expected_report_sha256': 'stale'})
    with monkeypatch.context() as changed:
        changed.setattr(closure, 'inspect_reservations', lambda *_: {
            'unknown': {**evidence['unknown'], 'reason': 'record_changed'}})
        with pytest.raises(ValueError, match='preview digest changed'):
            closure.recover(service, request)

    def unavailable(*_):
        raise OSError('session storage temporarily unavailable')

    with monkeypatch.context() as inaccessible:
        inaccessible.setattr(closure, 'inspect_reservations', unavailable)
        with pytest.raises(OSError, match='session storage temporarily unavailable'):
            closure.recover(service, request)
        assert control.status() == head
        assert not (base/'closure-recoveries/review.json').exists()
    finish = closure.finish
    def interrupted_after_confirmation(*_):
        raise RuntimeError('simulated restart after confirmation')
    monkeypatch.setattr(closure, 'finish', interrupted_after_confirmation)
    with pytest.raises(RuntimeError, match='simulated restart'):
        closure.recover(service, request)
    monkeypatch.setattr(closure, 'finish', finish)
    assert closure.recover(service, {**review['request'], 'boundary_fingerprint': None,
                           'expected_revision': control.status()['revision']}, preview=True) == review
    result = closure.recover(service, request)
    with monkeypatch.context() as inaccessible:
        inaccessible.setattr(closure, 'inspect_reservations', unavailable)
        assert result == closure.recover(service, request)
    assert result['closure']['state'] == 'partial' and result['closure']['retained_reservation_ids'] == ['unknown']
    assert len(list((base/'operations').glob('close-accounted-*.json'))) == 1
    if historical:
        assert (base/'runtime-requests/close-fixture.json').read_bytes() == old_intent


def test_budget_cannot_reset_by_restarting_and_unknown_consumption_blocks_closure(incremental):
    service, request, _, _, _, _, _ = incremental
    source = seal(incremental)
    map_source(incremental, source)
    request_again = dict(request)
    assert service.start(request_again)['revision'] > 1
    reservation = {'request_fingerprint': 'second', 'phase': 'mapping', 'max_calls': 2,
                   'max_input_tokens': 100, 'max_output_tokens': 100}
    with pytest.raises(ContractError, match='phase call budget exhausted'):
        service.reserve_budget(mutation(service, 'over-budget', reservation=reservation))
    assert len(service.status('fixture')['reservations']) == 1
    with pytest.raises(ContractError, match='unfinished'):
        service.close_run(mutation(service, 'close-too-early'))


def test_request_idempotency_and_round_single_owner(incremental):
    service = incremental[0]
    source = seal(incremental)
    mapped = map_source(incremental, source)
    request = mutation(service, 'freeze-1', round_id='round-1', max_maps=1)
    first = service.freeze_round(request)
    assert service.freeze_round(request) == first
    changed = {**request, 'round_id': 'round-2'}
    with pytest.raises(ContractError, match='operation key identifies different inputs'):
        service.freeze_round(changed)
    with pytest.raises(ContractError, match='active round'):
        service.freeze_round(mutation(service, 'freeze-2', round_id='round-2', max_maps=1))
    service.settle_round(mutation(service, 'settle-1', round_id='round-1', disposition={
        'round_input_fingerprint': first['input_fingerprint'], 'state': 'held', 'reason': 'Missing required source B'}))
    assert mapped['map_result_id'] in service.status('fixture')['consumed_map_ids']
    with pytest.raises(ContractError, match='no unconsumed'):
        service.freeze_round(mutation(service, 'freeze-2', round_id='round-2', max_maps=1))


def test_new_results_cannot_bypass_current_control(incremental):
    service = incremental[0]
    service.check_admission = lambda scope: {'allowed': False, 'input_fingerprint': scope['input_fingerprint']}
    with pytest.raises(ContractError, match='did not admit'):
        seal(incremental)
    assert service.status('fixture')['revision'] == 1
