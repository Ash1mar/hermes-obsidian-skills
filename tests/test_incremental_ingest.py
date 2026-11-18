"""Independent source sealing, finite rounds and durable cumulative consumption."""
from pathlib import Path
import hashlib
import json

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
