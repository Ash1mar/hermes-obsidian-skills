"""Source-runtime restart keeps complete lineage and exact qualification intent."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/scripts'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-controlled-ingest/scripts'))
import incremental_runtime as runtime
import incremental_native
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def test_mapping_restart_collects_previously_finished_producers(tmp_path, monkeypatch):
    import prepare_short_evidence_test as planner
    import run_short_evidence_test as native

    snapshot = {'snapshot_id': 'snapshot-fixture', 'identity': {'resource_id': 'resource'},
                'original': {'ref': '10_Raw/a.pdf'}}
    root = tmp_path / 'native'
    root.mkdir()
    jobs = []
    for i in (1, 2):
        key = str(i) * 64
        row = {'id': key, 'phase': 'short-topics' if i == 1 else 'short-map'}
        path = root / 'jobs' / (key + '.json')
        put(path, {'request': row})
        put(path.with_suffix('.result.json'), {'state': 'settled'})
        jobs.append(row)
    unit = {'resource_id': 'resource', 'unit_set_id': 'units', 'unit_id': 'unit'}
    sources = {'S1': {'items': {'S1-B1': {'source_ref': {'unit_ref': unit, 'span': None}, 'sha256': 'bytes'}}}}
    seen = []
    run = {'actor': 'actor', 'source_manifest': [{'path': '10_Raw/a.pdf'}]}
    domain = SimpleNamespace(vault=tmp_path, validate_source_snapshot=lambda *_: snapshot, status=lambda _: run)

    class Control:
        @contextmanager
        def executing(self, *args):
            yield {'allowed': True}

    monkeypatch.setattr(runtime, 'service', lambda *_: domain)
    monkeypatch.setattr(runtime, 'IncrementalControl', lambda *_: Control())
    monkeypatch.setattr(runtime, 'initialize_scope', lambda *_: (root, {}))
    monkeypatch.setattr(runtime, 'qualify', lambda *_: None)
    monkeypatch.setattr(runtime, 'source_pin', lambda _: {})
    monkeypatch.setattr(runtime, 'load_sources', lambda *_a, **_k: sources)
    monkeypatch.setattr(runtime, 'seed_requests', lambda _: [])
    monkeypatch.setattr(runtime, 'mapping_groups', lambda *_: [])
    monkeypatch.setattr(planner, 'advance', lambda *_a, **_k: {'stage': 'coordination', 'reduced': {
        'dispositions': {'S1-B1': {'status': 'excluded', 'reason': 'non-substantive context'}}, 'unresolved': []}})

    def execute(_domain, _run, _root, _operation, rows):
        seen.extend(row['id'] for row in rows)
        return [(row, {'state': 'settled'}, {'task_id': row['id']}) for row in rows]

    monkeypatch.setattr(runtime, 'execute_rows', execute)
    monkeypatch.setattr(native, 'task_body', lambda _root, path: json.dumps({'job': path.name}))

    def mutate(_domain, _run, method, _operation_id, **fields):
        if method == 'reserve_budget':
            return {'budget_id': 'budget'}
        return fields

    monkeypatch.setattr(runtime, 'mutate', mutate)
    result = runtime.map_source(tmp_path, 'run', snapshot['snapshot_id'])
    assert seen == [row['id'] for row in jobs]
    assert len(result['native_completions']) == 2
    assert result['coverage'][0]['disposition'] == 'excluded'
    assert sources['S1']['incremental_snapshot_id'] == snapshot['snapshot_id']


def test_qualification_replays_original_transaction_after_registry_moves(tmp_path, monkeypatch):
    import governance_repository

    run_id, snapshot_id = 'run', 'snapshot'
    operation_id = 'incremental-source-' + runtime.fingerprint([run_id, snapshot_id])[:48]
    request = {'incremental_run_id': run_id, 'snapshot_id': snapshot_id, 'expected_registry_revision': 3}
    transaction = tmp_path / '_system/metadata/source-governance-resolutions' / operation_id / 'transaction.json'
    put(transaction, {'request': request})
    calls = []
    repo = SimpleNamespace(load_state=lambda: pytest.fail('current registry must not replace saved request'),
                           resolve_sources=lambda q: calls.append(q) or {'ok': True, 'resolved': 1, 'replayed': True})
    monkeypatch.setattr(governance_repository, 'JsonGovernanceRepository', lambda _: repo)
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path / 'run',
                              validate_source_snapshot=lambda *_: {'identity': {'resource_id': 'resource'}},
                              status=lambda _: {'source_workflow_id': 'ingest-run'})
    assert runtime.qualify(domain, run_id, snapshot_id)['resolved'] == 1
    assert calls == [request]
    saved = json.loads((tmp_path / 'run/source-qualification/snapshot/receipt.json').read_text())
    assert 'replayed' not in saved


def test_runtime_mutation_replay_preserves_original_revision_and_checks_inputs(tmp_path):
    state = {'actor': 'operator', 'revision': 1}
    calls = []

    def seal(request):
        calls.append(dict(request))
        put(tmp_path / 'operations/seal-a.json', {'state': 'committed'})
        return {'snapshot_id': 'snapshot-a'}

    domain = SimpleNamespace(root=lambda _: tmp_path, status=lambda _: state,
                             seal_source_snapshot=seal,
                             record_source_failure=lambda _: pytest.fail('changed method must be rejected'))
    fields = {'source_path': '10_Raw/a.pdf', 'outcome': {'status': 'ready'}}
    first = incremental_native.mutate(domain, 'run', 'seal_source_snapshot', 'seal-a', **fields)
    state['revision'] = 7  # Other independent source mutations have committed.
    assert incremental_native.mutate(domain, 'run', 'seal_source_snapshot', 'seal-a', **fields) == first
    assert [request['expected_revision'] for request in calls] == [1, 1]
    saved = json.loads((tmp_path / 'runtime-requests/seal-a.json').read_text())
    assert saved['request']['expected_revision'] == 1
    with pytest.raises(ContractError, match='different inputs'):
        incremental_native.mutate(domain, 'run', 'seal_source_snapshot', 'seal-a',
                                  **{**fields, 'source_path': '10_Raw/b.pdf'})
    with pytest.raises(ContractError, match='different inputs'):
        incremental_native.mutate(domain, 'run', 'record_source_failure', 'seal-a', **fields)
    assert len(calls) == 2


def test_runtime_intent_without_canonical_journal_can_refresh_only_its_revision(tmp_path):
    state = {'actor': 'operator', 'revision': 2}
    calls = []

    def reserve(request):
        calls.append(dict(request))
        if len(calls) == 1:
            raise OSError('isolated interruption before canonical domain journal')
        put(tmp_path / 'operations/reserve-a.json', {'state': 'committed'})
        return {'budget_id': 'budget-a'}

    domain = SimpleNamespace(root=lambda _: tmp_path, status=lambda _: state, reserve_budget=reserve)
    fields = {'reservation': {'request_fingerprint': 'semantic-request-a', 'max_calls': 1}}
    with pytest.raises(OSError):
        incremental_native.mutate(domain, 'run', 'reserve_budget', 'reserve-a', **fields)
    assert not (tmp_path / 'operations/reserve-a.json').exists()
    state['revision'] = 4
    assert incremental_native.mutate(domain, 'run', 'reserve_budget', 'reserve-a', **fields) == {'budget_id': 'budget-a'}
    assert [request['expected_revision'] for request in calls] == [2, 4]
    assert calls[0]['reservation'] == calls[1]['reservation'] == fields['reservation']
    saved = json.loads((tmp_path / 'runtime-requests/reserve-a.json').read_text())
    assert saved['request'] == calls[-1]
