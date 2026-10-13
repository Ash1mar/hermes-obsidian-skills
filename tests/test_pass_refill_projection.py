"""Isolated leaf-Pass projection order, authority and bounded capacity."""
from __future__ import annotations

import copy
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-source-units/src'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-controlled-ingest/lib'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/lib'))

from hermes_source_units import ContractError, runtime_telemetry
from hermes_source_units.validation import fingerprint
import ingest_kanban as module
import program_worker


@contextmanager
def no_span(*args, **kwargs):
    yield {}


@pytest.fixture
def projection(tmp_path, monkeypatch):
    """Only synthetic control-plane JSON; no workers or domain products."""
    for name in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, '_exclusive_lock', lambda *a, **kw: nullcontext())
    monkeypatch.setattr(module, 'span', no_span)
    monkeypatch.setattr(runtime_telemetry, 'span', no_span)
    events = []
    value = {'workflow_id': 'ingest-refill-projection', 'actor': 'agent', 'revision': 1,
        'cancel_requested': False, 'batch_id': 'batch', 'current_stage': 'analyzing',
        'scope': {'pause_after': ['pass']}, 'pause_control': {'boundary': None},
        'kanban': {'board_id': 'board', 'task_map': []}}
    slices = {}
    native = {}
    snapshots = {}
    bindings = {}
    paths = {}
    for number in range(1, 5):
        sid = 's' + str(number)
        tid = 't' + str(number)
        item = {'slice_id': sid, 'state': 'completed' if number == 1 else 'leased' if number == 2 else 'ready',
            'input_fingerprint': 'sha256:' + str(number) * 64, 'template_hash': 'a' * 64,
            'lease': {'worker_id': 'ingest-worker-' + tid if number == 2 else None}}
        slices[sid] = item
        node = module.pass_node(value, item)
        key = f"ingest:{value['workflow_id']}:pass-slice:{node.input_fingerprint}"
        card = {'task_id': tid, 'node': node.name, 'idempotency_key': key}
        value['kanban']['task_map'].append(card)
        path = tmp_path / '_system/ledgers/ingest-workflows' / value['workflow_id'] / 'bindings' / (fingerprint(key) + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        request = {'workflow_id': value['workflow_id'], 'task_id': tid, 'node': node.name,
            'actor': value['actor'], 'worker_id': 'ingest-worker-' + tid,
            'input_fingerprint': node.input_fingerprint, 'template_hash': item['template_hash']}
        path.write_text(json.dumps(request), encoding='utf-8')
        bindings[tid] = request
        paths[tid] = path
        body = {'workflow_id': value['workflow_id'], 'node': node.name, 'vault': str(tmp_path),
            'input_fingerprint': node.input_fingerprint, 'worker_binding': str(path),
            'slice_template_hash': item['template_hash'], 'executor': 'semantic-v1'}
        native[tid] = {'id': tid, 'status': 'running' if number <= 2 else 'blocked',
            'created_by': 'ingest-workflow', 'assignee': module.PROGRAM_ASSIGNEE,
            'idempotency_key': key, 'body': json.dumps(body), 'current_run_id': number,
            'worker_pid': 100 + number if number <= 2 else None, 'claim_lock': f'claim-{number}'}
        snapshots[tid] = {'task': native[tid], 'children': [], 'runs': []}
    paths['t1'].with_suffix('.execution.json').write_text(json.dumps(
        {'state': 'completed', 'run_id': 1, 'result': {'ok': True},
         'grant': {'claim_lock': 'claim-1', 'worker_pid': 101}}), encoding='utf-8')
    batch = {'slice_config': {'pass_worker_concurrency': 2}, 'cancel_requested': False,
        'cooldown_until': None}

    def status(wid):
        assert wid == value['workflow_id']
        return value

    def latch(request):
        events.append('pause-latch')
        return value

    def states(board):
        events.append('native-states')
        return {tid: task['status'] for tid, task in native.items()}

    def complete(board, tid, result):
        events.append('ack:' + tid)
        native[tid]['status'] = 'done'
        native[tid]['worker_pid'] = None

    def unblock(board, tid):
        events.append('unblock:' + tid)
        native[tid]['status'] = 'ready'

    workflow = SimpleNamespace(vault=tmp_path, status=status,
        assert_execution_ready=lambda live: events.append('execution-ready'),
        execution_projection=lambda: nullcontext(), latch_pause=latch,
        pending_pause=lambda live: live['pause_control']['boundary'],
        _lock=lambda wid: tmp_path / (wid + '.lock'),
        knowledge=SimpleNamespace(_slices=lambda bid: list(slices.values()),
            _batch=lambda bid: batch, _slice=lambda bid, sid: slices[sid]))
    adapter = object.__new__(module.IngestKanbanAdapter)
    adapter.workflow = workflow
    adapter.enable_workers = True
    adapter.kanban = SimpleNamespace(task_states=states, projection=lambda board: nullcontext(),
        task_snapshot=lambda board, tid: copy.deepcopy(snapshots[tid]),
        task_snapshot_fresh=lambda board, tid: copy.deepcopy(snapshots[tid]),
        complete=complete, unblock=unblock)

    def settle(live):
        events.append('settle-ended')
        if slices['s2']['state'] == 'leased':
            slices['s2']['state'] = 'failed'
            slices['s2']['lease']['worker_id'] = None
            native['t2']['status'] = 'archived'
            native['t2']['worker_pid'] = None

    def admission(wid, node, tid, **kwargs):
        assert not value['cancel_requested'] and not value['pause_control']['boundary']
        assert slices[node.partition(':')[2]]['state'] == 'ready'
        events.append('admission:' + tid)

    def canonical(adapter, path, **kwargs):
        return json.loads(Path(path).read_text(encoding='utf-8')), value

    monkeypatch.setattr(program_worker, 'canonical_binding', canonical)
    monkeypatch.setattr(adapter, '_settle_ended_pass_leases', settle)
    monkeypatch.setattr(adapter, '_slice_worker', admission)
    monkeypatch.setattr(adapter, '_canary_allows', lambda live, node: not live['pause_control']['boundary'])
    failed = set()
    monkeypatch.setattr(adapter, '_failed_card', lambda wid, node: node.name in failed)
    return SimpleNamespace(adapter=adapter, value=value, slices=slices, native=native,
        snapshots=snapshots, batch=batch, events=events, paths=paths, failed=failed)


def run(projection):
    projection.adapter.reconcile_pass_workers(projection.value['workflow_id'])


def test_completion_and_ended_lease_settlement_precede_fresh_refill(projection):
    run(projection)
    events = projection.events
    assert events.index('pause-latch') < events.index('ack:t1') < events.index('settle-ended')
    assert events.index('settle-ended') < events.index('admission:t3') < events.index('unblock:t3')
    assert events[-4:] == ['admission:t3', 'unblock:t3', 'admission:t4', 'unblock:t4']
    assert projection.slices['s2']['state'] == 'failed'
    assert projection.slices['s2']['lease']['worker_id'] is None
    assert projection.events.count('native-states') == 2


def test_latched_pause_acknowledges_completed_current_run_without_refill(projection):
    projection.value['pause_control']['boundary'] = 'pass'
    run(projection)
    assert 'ack:t1' in projection.events
    assert 'settle-ended' not in projection.events
    assert not any(event.startswith(('admission:', 'unblock:')) for event in projection.events)


def test_cancellation_has_no_projection_or_refill_action(projection):
    projection.value['cancel_requested'] = True
    run(projection)
    assert not projection.events


def test_boundary_reached_after_latch_cannot_ack_or_refill_before_durable_pause(projection, monkeypatch):
    monkeypatch.setattr(projection.adapter.workflow, 'pending_pause', lambda value: 'pass')
    run(projection)
    assert 'ack:t1' not in projection.events
    assert 'settle-ended' not in projection.events
    assert not any(event.startswith(('admission:', 'unblock:')) for event in projection.events)


@pytest.mark.parametrize('name', ['HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'])
def test_worker_context_cannot_reconcile_or_refill(projection, monkeypatch, name):
    monkeypatch.setenv(name, 'isolated-worker')
    with pytest.raises(ContractError, match='ACCESS_DENIED'):
        run(projection)
    assert not projection.events


def test_leased_and_native_inflight_same_node_count_once_at_concurrency_two(projection, monkeypatch):
    projection.slices['s1']['state'] = 'leased'
    projection.slices['s1']['lease']['worker_id'] = 'ingest-worker-t1'
    # t1 occupies both native and domain capacity; t2 is no longer running.
    projection.slices['s2']['state'] = 'failed'
    projection.slices['s2']['lease']['worker_id'] = None
    projection.native['t2']['status'] = 'archived'
    monkeypatch.setattr(projection.adapter, '_settle_ended_pass_leases', lambda value: None)
    run(projection)
    assert [e for e in projection.events if e.startswith('unblock:')] == ['unblock:t3']


def test_two_distinct_native_inflight_nodes_leave_no_refill_capacity(projection, monkeypatch):
    projection.slices['s1']['state'] = 'leased'
    projection.slices['s1']['lease']['worker_id'] = 'ingest-worker-t1'
    projection.slices['s2']['lease']['worker_id'] = None
    monkeypatch.setattr(projection.adapter, '_settle_ended_pass_leases', lambda value: None)
    run(projection)
    assert not any(event.startswith(('admission:', 'unblock:')) for event in projection.events)


@pytest.mark.parametrize('case', ['failed-report', 'non-leaf', 'domain-failed'])
def test_failed_or_nonleaf_card_is_not_refilled(projection, case):
    if case == 'failed-report':
        projection.failed.add('pass-slice:s3')
    elif case == 'non-leaf':
        projection.snapshots['t3']['children'] = [{'id': 'dependent-child'}]
    else:
        projection.slices['s3']['state'] = 'failed'
    run(projection)
    assert 'unblock:t3' not in projection.events
    assert 'unblock:t4' in projection.events


@pytest.mark.parametrize('case', ['old-run', 'failed-receipt', 'non-leaf', 'missing-grant'])
def test_old_or_failed_receipt_and_nonleaf_card_do_not_ack_completion(projection, case):
    path = projection.paths['t1'].with_suffix('.execution.json')
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if case == 'old-run':
        receipt['run_id'] = 99
    elif case == 'failed-receipt':
        receipt['result']['ok'] = False
    elif case == 'missing-grant':
        receipt.pop('grant')
    else:
        projection.snapshots['t1']['children'] = [{'id': 'dependent-child'}]
    path.write_text(json.dumps(receipt), encoding='utf-8')
    run(projection)
    assert 'ack:t1' not in projection.events


@pytest.mark.parametrize('field,value', [('current_run_id', 99), ('claim_lock', 'foreign-claim'),
                                       ('worker_pid', 999), ('status', 'archived')])
def test_fresh_native_grant_change_prevents_completion_ack(projection, monkeypatch, field, value):
    fresh_reads = []
    def fresh(board, tid):
        fresh_reads.append(tid)
        snapshot = copy.deepcopy(projection.snapshots[tid])
        snapshot['task'][field] = value
        return snapshot
    monkeypatch.setattr(projection.adapter.kanban, 'task_snapshot_fresh', fresh)
    run(projection)
    assert fresh_reads == ['t1']
    assert 'ack:t1' not in projection.events


@pytest.mark.parametrize('case', ['native-id', 'native-body', 'binding-template', 'binding-worker', 'binding-input'])
def test_foreign_or_stale_current_card_cannot_be_unblocked(projection, case):
    if case == 'native-id':
        projection.native['t3']['id'] = 'foreign'
    elif case == 'native-body':
        body = json.loads(projection.native['t3']['body'])
        body['input_fingerprint'] = 'sha256:' + '0' * 64
        projection.native['t3']['body'] = json.dumps(body)
    else:
        path = projection.paths['t3']
        binding = json.loads(path.read_text(encoding='utf-8'))
        field = {'binding-template': 'template_hash', 'binding-worker': 'worker_id',
                 'binding-input': 'input_fingerprint'}[case]
        binding[field] = 'foreign'
        path.write_text(json.dumps(binding), encoding='utf-8')
    with pytest.raises(ContractError, match='STALE_INPUT'):
        run(projection)
    assert 'unblock:t3' not in projection.events
