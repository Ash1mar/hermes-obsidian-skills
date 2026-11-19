"""Replay saved page coordination only through its bound original native prompt."""
import copy
from pathlib import Path
import sys

import pytest

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units import incremental_pages
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
import incremental_ingest_controller as controller


def test_coordination_splits_complete_topics_and_reuses_native_results(vault, tmp_path, monkeypatch):
    first, second = publish_sources(vault, ['# A\n\nFirst evidence.\n', '# B\n\nSecond evidence.\n'])
    service = FileKnowledgeBuildService(vault)
    frozen = {'contract': 'incremental-round-input/v1', 'run_id': 'inc', 'round_id': 'round-1',
              'map_result_ids': ['map-1'], 'snapshots': ['source-1'], 'input_fingerprint': fingerprint('round')}
    topics = {key: {'name': key, 'scope': key, 'identity': key, 'source': 'S1', 'relations': [
        {'item': alias, 'role': 'evidence', 'applies_to': [], 'note': 'mapped'}]}
        for key, alias in [('A', 'E1'), ('B', 'E2')]}
    maps = [{'map_result_id': 'map-1', 'snapshot_id': 'source-1', 'alias_table': {'E1': {}, 'E2': {}},
             'mapped': {'topics': topics}}]
    coordinator = tmp_path / 'native'
    (coordinator / 'receipts').mkdir(parents=True)
    calls = []

    def budget(spec):
        return {'id': fingerprint(spec), **spec, 'budget': {'fits': len(spec['view']['mapping_topics']) == 1}}

    def execute(_domain, _run_id, directory, _operation, rows):
        calls.append([row['id'] for row in rows])
        for row in rows:
            topic, = row['view']['mapping_topics']
            key = topic['name']
            alias = topic['relations'][0][0]
            answer = {'changes': [{'action': 'create', 'page_id': '', 'identity_key': key, 'name': key,
                                   'path': f'30_Cards/工程/{key}.md', 'page_type': 'card',
                                   'support_aliases': [alias], 'required_source_snapshots': ['source-1'],
                                   'reason': 'Scoped native decision'}]}
            (directory / 'receipts' / (row['id'] + '.json')).write_bytes(_json_bytes({'answer': answer}))

    monkeypatch.setattr(controller, 'bind_request', budget)
    monkeypatch.setattr(controller, 'execute_rows', execute)
    monkeypatch.setattr(controller, 'verified_result', lambda *_: ({'state': 'settled'}, {}))
    args = (None, 'inc', service, frozen, {'E1': first, 'E2': second}, maps, coordinator, {}, tmp_path / 'round')
    plan, gaps = controller.grouped_page_changes(*args)
    assert len(calls) == 1 and len(calls[0]) == 2
    assert not gaps and [row['name'] for row in plan] == ['A', 'B']
    assert controller.grouped_page_changes(*args) == (plan, gaps)
    assert len(calls) == 1


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
