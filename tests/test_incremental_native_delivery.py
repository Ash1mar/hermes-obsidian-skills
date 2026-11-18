"""Native receipt authenticity and explicit delivery recovery stay separate."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'hermes-obsidian-governed-ingest-orchestrator/scripts'))
sys.path.insert(0, str(REPO / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units.source_units import _json_bytes, _write_atomic
from hermes_source_units.validation import fingerprint
import incremental_native


def put(path, value):
    _write_atomic(path, _json_bytes(value))


def test_semantic_receipt_must_equal_the_audited_native_response(tmp_path, monkeypatch):
    import run_short_evidence_test as runner
    import fixed_semantic
    import fixed_semantic_recovery

    root = tmp_path / 'native'
    root.mkdir()
    operation = {'workflow_mode': 'incremental_v1', 'vault': str(tmp_path), 'board': 'board', 'contract': 'native'}
    put(root / 'operation.json', operation)
    row = {'id': 'request', 'phase': 'incremental-page-write'}
    job = root / 'jobs/request.json'
    put(job, {'request': row})
    grant = {'task_id': 'task', 'run_id': 1, 'board': 'board'}
    answer = {'results': {'r': {'content': 'native authored content'}}}
    audit = {'grant': grant, 'state': 'returned', 'model': {'parent_native_run_id': '1'},
             'raw': json.dumps(answer)}
    put(root / 'jobs/request.audit-1.json', audit)
    put(root / 'jobs/request.attempt-1.json', {'grant': grant, 'ordinal': 1,
                                              'request': {'id': row['id'], 'image_inputs': []}})
    result = {'state': 'settled', 'task_id': 'task', 'run_id': 1, 'request_id': row['id'],
              'attempts': [{**audit, 'ordinal': 1, 'capture': {'sealed': ['r'], 'held': {}}}]}
    put(job.with_suffix('.result.json'), result)
    receipt = root / 'receipts/request.json'
    put(receipt, {'contract': 'incremental-native-response/v1', 'request_fingerprint': row['id'],
                  'answer': answer['results']['r']})
    task = SimpleNamespace(id='task', created_by='native', status='done', claim_lock=None,
                           body=runner.task_body(root, job))
    kb = SimpleNamespace(get_task=lambda *_: task, list_runs=lambda *_a, **_k: [SimpleNamespace(id=1, outcome='completed')])
    kc = SimpleNamespace(connect=lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runner, 'runtime', lambda: (kb, kc, None))
    monkeypatch.setattr(runner, 'checked_projection', lambda _: {'wire': {'user': 'original'}})
    monkeypatch.setattr(fixed_semantic_recovery, 'transcript_reply', lambda *_: audit['raw'])
    monkeypatch.setattr(fixed_semantic, 'decode_semantic_response', lambda *_: (answer, None))
    assert incremental_native.verified_result(root, row)[0] == result
    put(receipt, {'contract': 'incremental-native-response/v1', 'request_fingerprint': row['id'],
                  'answer': {'content': 'modified content'}})
    with pytest.raises(ValueError, match='sealed incremental response'):
        incremental_native.verified_result(root, row)


def test_known_delivery_prefix_uses_explicit_recovery_and_original_build(tmp_path, monkeypatch):
    from hermes_source_units.incremental_sources import pinned
    import hermes_source_units.incremental_delivery as delivery_module
    import hermes_source_units.vault_finalize as finalize_module

    root = tmp_path / 'native'
    root.mkdir()
    operation = {'workflow_mode': 'incremental_v1', 'vault': str(tmp_path), 'board': 'board',
                 'incremental': {'run_id': 'run', 'scope_id': 'round', 'scope_kind': 'round', 'stage': 'formal-build'}}
    put(root / 'operation.json', operation)
    view = {'authorization': {'contract': 'incremental-build-authorization/v1'}, 'written': [], 'excluded': [],
            'provider_config': str(tmp_path / 'provider.json')}
    original = {'id': fingerprint(view), 'phase': 'incremental-formal-build', 'view': view}
    original_job = root / 'jobs/original.json'
    put(original_job, {'request': original})
    state_path = tmp_path / 'delivery.json'
    put(state_path, {})
    calls = []
    prior = {'state': 'index_pending', 'authorization': view['authorization'], 'build_run_id': 'same-build'}
    class Delivery:
        def __init__(self, *_a, **_k):
            pass
        def _path(self, *_):
            return state_path
        def status(self, *_):
            return prior
        def recover(self, run_id, round_id, binding, **kwargs):
            assert kwargs['disposition'] == 'continue'
            calls.append((run_id, round_id, binding))
            return {**prior, 'state': 'committed'}
    monkeypatch.setattr(delivery_module, 'IncrementalDeliveryService', Delivery)
    monkeypatch.setattr(finalize_module, 'FileVaultFinalizeService', lambda _: SimpleNamespace(vault=tmp_path))
    own = lambda: {'board': 'board', 'task_id': 'native', 'run_id': 9}
    runner = SimpleNamespace(sha=lambda _: 'operation')
    with pytest.raises(ValueError, match='explicit mechanical recovery'):
        incremental_native.build(root, original_job, original, own, runner)
    recovery_view = {**view, 'recovery': {'operation_id': 'recover-index',
                                        'original_job': pinned(tmp_path, original_job.relative_to(tmp_path).as_posix()),
                                        'disposition': 'continue', 'stop_after': 'index_verified'}}
    row = {'id': fingerprint(recovery_view), 'phase': original['phase'], 'view': recovery_view}
    recovered = incremental_native.build(root, root / 'jobs/recovery.json', row, own, runner)
    assert recovered['formal_run_id'] == 'same-build' and len(calls) == 1
    assert original_job.read_bytes() == _json_bytes({'request': original})
