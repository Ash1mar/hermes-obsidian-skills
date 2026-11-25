"""Source-runtime restart keeps complete lineage and exact qualification intent."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/scripts'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-controlled-ingest/scripts'))
import incremental_runtime as runtime
import incremental_native
from hermes_source_units import ContractError
from hermes_source_units.incremental_qualification import qualification_lock
from hermes_source_units.source_units import _exclusive_lock, _json_bytes


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def test_reusing_map_results_does_not_open_usage_for_settled_or_unattributable_attempts(tmp_path, monkeypatch):
    import workflow_metrics

    def unexpected_lookup(*args, **kwargs):
        pytest.fail(f'attempted session lookup for a known or unattributable result: {args!r} {kwargs!r}')

    monkeypatch.setattr(workflow_metrics, 'usage_for', unexpected_lookup)
    row = {'id': 'original-request'}
    reservations = {
        f'budget-{ordinal}': {'request_fingerprint': runtime.fingerprint({'request': row['id'], 'ordinal': ordinal}),
                              'state': state}
        for ordinal, state in ((1, 'settled'), (2, 'reserved'))
    }
    domain = SimpleNamespace(status=lambda _: {'reservations': reservations})
    runtime.settle_usage(domain, 'run', tmp_path, row, {'attempts': []})
    runtime.settle_usage(domain, 'run', tmp_path, row, {'attempts': [
        {'ordinal': 1, 'model': {'session_id': 'already-accounted-session'}},
        {'ordinal': 2, 'model': {'state': 'completion_unknown'}},
    ]})
    assert not (tmp_path / 'usage').exists()


@pytest.mark.parametrize('sample', [
    {},
    {'input': 0, 'output': 0},
    {'api_calls': 0, 'input': 0, 'output': 0},
    {'api_calls': 1, 'input': 1.5, 'output': 2},
    {'api_calls': 1, 'input': 101, 'output': 2},
])
def test_unverified_session_counters_preserve_the_original_map_reservation(tmp_path, monkeypatch, sample):
    import workflow_metrics

    row = {'id': 'original-request'}
    attempt = {'ordinal': 1, 'model': {'session_id': 'exact-session'}}
    reservation = {'request_fingerprint': runtime.fingerprint({'request': row['id'], 'ordinal': 1}),
                   'state': 'reserved', 'max_calls': 1, 'max_input_tokens': 100, 'max_output_tokens': 10}
    domain = SimpleNamespace(status=lambda _: {'reservations': {'budget': reservation}})

    def lookup(calls, *, active):
        assert calls == [attempt] and active
        return {'by_session': [{'session_id': 'exact-session', **sample}]}

    monkeypatch.setattr(workflow_metrics, 'usage_for', lookup)
    runtime.settle_usage(domain, 'run', tmp_path, row, {'attempts': [attempt]})
    assert reservation['state'] == 'reserved'
    assert not (tmp_path / 'usage').exists()


@pytest.mark.parametrize('first_request', [False, True], ids=('reuse', 'half-second-contention'))
def test_mapping_verifies_producers_and_dispatches_fresh_request_once(tmp_path, monkeypatch, first_request):
    import incremental_map_recovery as recovery
    import prepare_short_evidence_test as planner
    import run_short_evidence_test as native

    snapshot = {'snapshot_id': 'snapshot-fixture', 'identity': {'resource_id': 'resource'},
                'original': {'ref': '10_Raw/a.pdf'}}
    root = tmp_path / 'native'
    root.mkdir()
    jobs = []
    for i in (1, 2):
        key = str(i) * 64
        row = {'id': key, 'phase': 'short-topics' if i == 1 else 'short-map', 'view': {}}
        path = root / 'jobs' / (key + '.json')
        if not first_request or i == 1:
            put(path, {'request': row})
            put(path.with_suffix('.result.json'), {'state': 'settled'})
            put(root / 'receipts' / path.name, {'request_id': key})
        jobs.append(row)
    unit = {'resource_id': 'resource', 'unit_set_id': 'units', 'unit_id': 'unit'}
    sources = {'S1': {'items': {'S1-B1': {'source_ref': {'unit_ref': unit, 'span': None}, 'sha256': 'bytes'}}}}
    inspected, dispatched = [], []
    run = {'actor': 'actor', 'source_manifest': [{'path': '10_Raw/a.pdf'}],
           'sources': {'10_Raw/a.pdf': {'state': 'prepared'}}}
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, validate_source_snapshot=lambda *_: snapshot, status=lambda _: run)

    class Control:
        def status(self): return {'state': 'active'}

        @contextmanager
        def executing(self, *args):
            yield {'allowed': True}

    monkeypatch.setattr(runtime, 'service', lambda *_: domain)
    monkeypatch.setattr(runtime, 'IncrementalControl', lambda *_: Control())
    monkeypatch.setattr(runtime, 'initialize_scope', lambda *_: (root, {}))
    lock = tmp_path / 'native-dispatch.lock'
    if first_request:
        acquired = Event()

        def hold():
            with _exclusive_lock(lock):
                acquired.set()
                time.sleep(.53)

        holder = Thread(target=hold)
        holder.start()
        assert acquired.wait(5)

        def qualify(*_):
            with qualification_lock(lock, time.monotonic() + 5, lambda: None):
                pass
    else:
        def qualify(*_): pass
    monkeypatch.setattr(runtime, 'qualify', qualify)
    monkeypatch.setattr(runtime, 'source_pin', lambda _: {})
    monkeypatch.setattr(runtime, 'load_sources', lambda *_a, **_k: sources)
    monkeypatch.setattr(runtime, 'seed_requests', lambda _: [])
    monkeypatch.setattr(runtime, 'mapping_groups', lambda *_: [])
    def advance(*_a, map_receipt_ids, **_k):
        if first_request and jobs[1]['id'] not in map_receipt_ids:
            assert map_receipt_ids == {jobs[0]['id']}
            return {'stage': 'map', 'requests': jobs[1:]}
        assert map_receipt_ids == {row['id'] for row in jobs}
        return {'stage': 'coordination', 'reduced': {
            'dispositions': {'S1-B1': {'status': 'excluded', 'reason': 'non-substantive context'}}, 'unresolved': []}}

    monkeypatch.setattr(planner, 'advance', advance)

    def inspect(_root, _operation, rows):
        inspected.extend(row['id'] for row in rows)
        return [(row, {'state': 'settled'}, {'task_id': row['id']}) for row in rows], {}, [], {}

    def execute(_domain, _run, _root, _operation, rows, _settle_usage):
        for row in rows:
            path = root / 'jobs' / (row['id'] + '.json')
            assert not path.is_file()
            dispatched.append(row['id'])
            put(path, {'request': row})
            put(path.with_suffix('.result.json'), {'state': 'settled'})
            put(root / 'receipts' / path.name, {'request_id': row['id']})
        return [(row, {'state': 'settled'}, {'task_id': row['id']}) for row in rows], {}

    monkeypatch.setattr(recovery, 'inspect_map_rows', inspect)
    monkeypatch.setattr(recovery, 'execute_map_rows', execute)
    monkeypatch.setattr(runtime, 'settle_usage', lambda *_: None)
    monkeypatch.setattr(native, 'task_body', lambda _root, path: json.dumps({'job': path.name}))

    def mutate(_domain, _run, method, _operation_id, **fields):
        if method == 'reserve_budget':
            return {'budget_id': 'budget'}
        return fields

    monkeypatch.setattr(runtime, 'mutate', mutate)
    result = runtime.map_source(tmp_path, 'run', snapshot['snapshot_id'])
    assert inspected == [row['id'] for row in (jobs[:1] if first_request else jobs)]
    assert dispatched == ([jobs[1]['id']] if first_request else [])
    if first_request:
        holder.join(timeout=5)
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
                           resolve_sources=lambda q, **_: calls.append(q) or {'ok': True, 'resolved': 1, 'replayed': True})
    monkeypatch.setattr(governance_repository, 'JsonGovernanceRepository', lambda _: repo)
    monkeypatch.setattr(runtime, 'IncrementalControl', lambda *_: SimpleNamespace(status=lambda: {'state': 'active'}))
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
    with pytest.raises(ValueError, match='different inputs'):
        incremental_native.mutate(domain, 'run', 'seal_source_snapshot', 'seal-a',
                                  **{**fields, 'source_path': '10_Raw/b.pdf'})
    with pytest.raises(ValueError, match='different inputs'):
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
