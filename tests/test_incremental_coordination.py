"""Replay saved page coordination only through its bound original native prompt."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units import incremental_pages
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint

skill = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(skill / entry) for entry in ('lib', 'scripts')]
import incremental_ingest_controller as controller
import incremental_native
import run_short_evidence_test as runner
from incremental_coordination_repair import authorize, corrected_request


@pytest.mark.parametrize('repair_result', [None, 'settled', 'unconfirmed', 'unacknowledged-file', 'foreign-scope',
                                           'missing-evidence', 'budget', 'previous-correction'])
def test_coordination_splits_complete_topics_and_reuses_native_results(vault, tmp_path, monkeypatch, repair_result):
    first, second = publish_sources(vault, ['# A\n\nFirst evidence.\n', '# B\n\nSecond evidence.\n'])
    service = FileKnowledgeBuildService(vault)
    frozen = {'contract': 'incremental-round-input/v1', 'run_id': 'inc', 'round_id': 'round-1',
              'map_result_ids': ['map-1', 'map-2'], 'snapshots': ['source-1', 'source-2'],
              'input_fingerprint': fingerprint('round')}
    topics = {key: {'name': key, 'scope': key, 'identity': key, 'source': 'S1', 'relations': [
        {'item': alias, 'role': 'evidence', 'applies_to': [], 'note': 'mapped'}]}
        for key, alias in [('A', 'E1'), ('B', 'E2')]}
    maps = []
    for index, (name, ref) in enumerate(zip(topics, [first, second]), 1):
        key = ref['unit_ref']
        _, units, _ = service.source._load_set(key['resource_id'], key['unit_set_id'])
        unit = next(row for row in units if row['ref'] == key)
        snapshot = f'source-{index}'
        maps.append({'map_result_id': f'map-{index}', 'snapshot_id': snapshot, 'mapped': {'topics': {name: topics[name]}},
                     'alias_table': {f'E{index}': {'unit_ref': key, 'snapshot_id': snapshot,
                                                  'sha256': unit['content_sha256']}},
                     'coverage': [{'unit_ref': key, 'disposition': 'mapped'}]})
    coordinator = tmp_path / 'native'
    (coordinator / 'receipts').mkdir(parents=True)
    (coordinator / 'jobs').mkdir()
    calls = []
    corrective_ids = set()
    original_bind = controller.bind_request

    def budget(spec):
        row = original_bind(spec)
        row['budget']['fits'] = len(spec['view']['mapping_topics']) == 1
        return row

    def execute(_domain, _run_id, directory, _operation, rows):
        calls.append([row['id'] for row in rows])
        for row in rows:
            topic, = row['view']['mapping_topics']
            key = topic['name']
            alias = topic['relations'][0][0]
            change = {'action': 'create', 'page_id': '', 'identity_key': key, 'name': key,
                      'path': f'30_Cards/工程/{key}.md', 'page_type': 'card',
                      'support_aliases': [alias], 'required_source_snapshots': [f"source-{1 if key == 'A' else 2}"],
                      'reason': 'Scoped native decision'}
            if row['view'].get('coordination_repair'):
                corrective_ids.add(row['id'])
                if repair_result == 'unconfirmed':
                    continue
                change['target'] = row['view']['targets'][0]['target']
                if repair_result == 'foreign-scope':
                    change['support_aliases'] = ['E2']
            elif repair_result and key == 'A':
                change.update(action='hold', reason='待读取冻结源完整证据。')
                if repair_result == 'missing-evidence':
                    change['reason'] = '待读取冻结源完整证据，但缺少必要的表格。'
            answer = {'changes': [change]}
            (directory / 'receipts' / (row['id'] + '.json')).write_bytes(_json_bytes({'answer': answer}))
            (directory / 'jobs' / (row['id'] + '.result.json')).touch()

    monkeypatch.setattr(controller, 'bind_request', budget)
    monkeypatch.setattr(controller, 'execute_rows', execute)
    def verified(_, row):
        if repair_result == 'unacknowledged-file' and row['id'] in corrective_ids:
            raise incremental_native.NativeCompletionUnconfirmed('incremental result lacks native completion')
        attempts = [{'state': 'returned'}] * (2 if repair_result == 'previous-correction' else 1)
        return {'state': 'settled', 'attempts': attempts}, {}

    monkeypatch.setattr(controller, 'verified_result', verified)
    ledger = {'reservations': {}, 'budget': {'max_calls': 20, 'max_input_tokens': 200000,
                                             'max_output_tokens': 100000,
                                             'phase_limits': {'coordination': 0 if repair_result == 'budget' else 10}}}
    args = (SimpleNamespace(status=lambda _: ledger), 'inc', service, frozen, {'E1': first, 'E2': second}, maps, coordinator,
            {'actor': 'agent'}, tmp_path / 'round')
    plan, gaps = controller.grouped_page_changes(*args)
    local = repair_result in ('settled', 'unconfirmed', 'unacknowledged-file', 'foreign-scope')
    assert len(calls) == (2 if local else 1) and len(calls[0]) == 2
    if local:
        assert len(calls[1]) == 1
    assert not gaps and sorted(row['name'] for row in plan) == ['A', 'B']
    assert next(row['action'] for row in plan if row['name'] == 'B') == 'create'
    assert next(row['action'] for row in plan if row['name'] == 'A') == (
        'create' if repair_result in (None, 'settled') else 'hold')
    assert controller.grouped_page_changes(*args) == (plan, gaps)
    assert len(calls) == (2 if local else 1)


@pytest.mark.parametrize('lock_failures', [1, 2])
def test_native_correction_has_one_ordinal_and_one_program_retry(tmp_path, monkeypatch, lock_failures):
    schema = {'type': 'object', 'required': ['changes'], 'properties': {'changes': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'action': {'type': 'string'}}, 'required': ['action']}}}}
    original = controller.bind_request({'phase': 'incremental-page-plan', 'schema': schema, 'view': {
        'round': 'round-1', 'planning_revision': 4, 'registered_pages': [],
        'registered_page_snapshot_sha256': fingerprint([]), 'mapping_sources': [], 'mapping_topics': [],
        'relation_columns': [], 'instructions': 'Plan from the frozen Map.'}})
    answer = {'changes': [{'action': 'hold'}]}
    target = {'name': 'A', 'required_aliases': ['E1'], 'allowed_aliases': ['E1'], 'required_source_snapshots': ['S1']}
    targets = {key: target for key in ('2', '10')}
    corrected = corrected_request(original, answer, targets)
    assert [item['target'] for item in corrected['view']['targets']] == ['2', '10']
    assert corrected == corrected_request(original, answer, json.loads(_json_bytes(targets)))
    for name in ('jobs', 'receipts', 'coordination-repairs'):
        (tmp_path / name).mkdir()
    (tmp_path / 'operation.json').write_bytes(_json_bytes({'workflow_mode': 'incremental_v1',
                                                          'incremental': {'stage': 'coordination'}}))
    stem = original['id'] + '.json'
    (tmp_path / 'jobs' / stem).write_bytes(_json_bytes({'request': original}))
    (tmp_path / 'receipts' / stem).write_bytes(_json_bytes({'answer': answer}))
    (tmp_path / 'coordination-repairs' / stem).write_bytes(_json_bytes({'targets': targets, 'request_id': corrected['id']}))
    job = tmp_path / 'jobs' / (corrected['id'] + '.json')
    job.write_bytes(_json_bytes({'request': corrected}))
    monkeypatch.setattr(incremental_native, 'verified_result', lambda *_: (
        {'state': 'settled', 'attempts': [{'state': 'returned'}]}, {}))
    with pytest.raises(ValueError, match='single ordinal'):
        authorize(tmp_path, corrected, 2)

    reservations, sent = [], []

    def reserve(root, row, ordinal, grant):
        authorize(root, row, ordinal)
        reservations.append((ordinal, grant))
        if len(reservations) <= lock_failures:
            raise ContractError('LOCK_TIMEOUT', '$', 'fixture contention')

    class Caller:
        def request_scope(self, _):
            return nullcontext()

        def __call__(self, *args):
            sent.append(args)
            return '{}', {}

    monkeypatch.setattr(incremental_native, 'reserve_call', reserve)
    monkeypatch.setattr(runner, 'HermesCaller', Caller)
    grant = {'task_id': 'task', 'run_id': 1, 'board': 'fixture'}
    if lock_failures == 1:
        result = runner.run_job(tmp_path, job, lambda: grant)
        assert result['state'] == 'held'
        assert [attempt['ordinal'] for attempt in result['attempts']] == [1]
    else:
        with pytest.raises(ContractError, match='fixture contention'):
            runner.run_job(tmp_path, job, lambda: grant)
    assert reservations == [(1, grant), (1, grant)]
    assert len(sent) == 2 - lock_failures
    assert job.with_name(job.stem + '.program-retry-1.json').is_file()


def test_saved_coordination_rechecks_native_result_without_rebasing_parent(vault, tmp_path, monkeypatch):
    ref, = publish_sources(vault, ['# Evidence\n\nA complete required condition.\n'])
    service = FileKnowledgeBuildService(vault)
    frozen = {'contract': 'incremental-round-input/v1', 'run_id': 'inc', 'round_id': 'round-1',
              'map_result_ids': ['map-1'], 'snapshots': ['source-1'], 'input_fingerprint': fingerprint('round')}
    maps = [{'map_result_id': 'map-1', 'snapshot_id': 'source-1'}]
    spec = incremental_pages.plan_page_changes(service, frozen, maps)
    row = {'id': fingerprint(spec), **spec}
    native = tmp_path / 'native'
    (native / 'receipts').mkdir(parents=True)
    answer = {'changes': [{'action': 'create', 'page_id': '', 'identity_key': 'scope/one', 'name': 'A',
                           'path': '30_Cards/工程/A.md', 'page_type': 'card', 'support_aliases': ['E1'],
                           'required_source_snapshots': ['source-1'], 'reason': 'All mapped evidence is covered'}]}
    (native / 'receipts' / (row['id'] + '.json')).write_bytes(_json_bytes({'answer': answer}))
    calls = []

    def verified(directory, request):
        calls.append((directory, request['id']))
        return {'state': 'settled'}, {'native_completion': 'fixture'}

    monkeypatch.setattr(controller, 'verified_result', verified)
    plan_path = tmp_path / 'page-changes.json'
    changes = controller.verified_page_changes(service, frozen, {'E1': ref}, maps, native, row, plan_path)
    pinned = plan_path.read_bytes()
    # A successful later Build can claim this path. Replaying coordination must
    # retain its original create decision rather than fabricate a new parent.
    target = vault / '30_Cards/工程/A.md'
    target.parent.mkdir(parents=True)
    target.write_text('# Already committed\n', encoding='utf-8')
    assert controller.verified_page_changes(service, frozen, {'E1': ref}, maps, native, row, plan_path) == changes
    assert plan_path.read_bytes() == pinned and len(calls) == 2
    with pytest.raises(ContractError, match='occupied'):
        incremental_pages.prepare_page_revision(service, changes[0], 'agent')
    tampered = copy.deepcopy(changes)
    tampered[0]['reason'] = 'Unbound manual change'
    plan_path.write_bytes(_json_bytes(tampered))
    with pytest.raises(ValueError, match='different identity'):
        controller.verified_page_changes(service, frozen, {'E1': ref}, maps, native, row, plan_path)
    assert len(calls) == 3
    plan_path.write_bytes(pinned)

    def rejected(*_):
        raise ValueError('native completion changed')

    monkeypatch.setattr(controller, 'verified_result', rejected)
    with pytest.raises(ValueError, match='native completion changed'):
        controller.verified_page_changes(service, frozen, {'E1': ref}, maps, native, row, plan_path)
    assert plan_path.read_bytes() == pinned
