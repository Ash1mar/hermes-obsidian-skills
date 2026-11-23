"""Modeled transport pause through the native short-path scheduling boundary."""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

import run_short_evidence_test as runner
from model_transport import TransportCircuitOpen

OPEN = {'route_id': 'route', 'trip_id': 'trip', 'probe_state': None, 'state': 'open'}


def operation(root):
    return {'board': 'native-board', 'network_recovery': True, 'vault': str(root),
            'workflow_mode': 'incremental_v1', 'incremental': {'stage': 'writing',
                'scope_kind': 'round', 'scope_id': 'round-1', 'run_id': 'run-id'}}


def native_fixture(root, monkeypatch, state, on_return):
    observed = []
    tasks = {}

    def create(_connection, *, idempotency_key, body, **_other):
        tasks.setdefault(idempotency_key, SimpleNamespace(status='ready', body=body, claim_lock=None))
        return idempotency_key

    def claim(_connection, task_id, **_):
        task = tasks[task_id]
        task.status, task.current_run_id, task.claim_lock = 'running', 1, 'lease'
        return task

    def spawn(argv, *, env, **_):
        job = Path(argv[-1])
        observed.append((job.stem, env.get(runner.NETWORK_PROBE_ENV)))

        def poll():
            tasks[job.stem].status = 'done'
            runner.save(job.with_suffix('.result.json'), {'state': 'held', 'request_id': job.stem,
                                                           'task_id': job.stem, 'attempts': []})
            on_return(observed)
            return 0

        return SimpleNamespace(pid=10, poll=poll)

    kb = SimpleNamespace(create_task=create, get_task=lambda _conn, task: tasks[task],
                         claim_task=claim, kanban_db_path=lambda **_: root / 'kanban.db')
    kc = SimpleNamespace(connect=lambda **_: SimpleNamespace(close=lambda: None))
    kd = SimpleNamespace(configured_max_in_progress=lambda: None,
                         resolve_max_in_progress=lambda value: value, _memory_pressure_level=lambda: 'normal',
                         _open_worker_log=lambda *_: nullcontext(None), _restart_safe_worker_argv=lambda _task, argv: argv,
                         _set_worker_pid=lambda *_: None)
    monkeypatch.setattr(runner, 'runtime', lambda: (kb, kc, kd))
    monkeypatch.setattr(runner, 'live', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, 'open_network_circuit', lambda _scope: state.get('open'))
    monkeypatch.setattr(runner.subprocess, 'Popen', spawn)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    return observed


def test_refill_stops_and_drains_before_saving_unstarted_jobs(tmp_path, monkeypatch):
    op = operation(tmp_path)
    runner.save(tmp_path / 'operation.json', op)
    status = {}

    def third_failure(observed):
        if len(observed) == 3:
            status['open'] = OPEN

    dispatched = native_fixture(tmp_path, monkeypatch, status, third_failure)
    rows = [{'id': str(number), 'phase': 'incremental-page-write'} for number in range(5)]
    with pytest.raises(runner.NetworkStagePaused) as paused:
        runner.stage(tmp_path, op, rows, 1)
    assert paused.value.root == tmp_path and paused.value.circuit == OPEN
    assert dispatched == [('0', None), ('1', None), ('2', None)]
    assert all((tmp_path / 'jobs' / (row['id'] + '.json')).is_file() for row in rows)
    assert not (tmp_path / 'stop.json').exists()


def test_operator_probe_uses_one_original_row_and_leaves_stop_marker_clear(tmp_path, monkeypatch):
    op = operation(tmp_path)
    runner.save(tmp_path / 'operation.json', op)
    status = {'open': OPEN}
    dispatched = native_fixture(tmp_path, monkeypatch, status, lambda _: status.clear())
    rows = [{'id': 'waiting', 'phase': 'incremental-page-write'}]
    probe = {'route_id': OPEN['route_id'], 'trip_id': OPEN['trip_id'],
             'operation_id': 'operator-continue', 'request_id': 'waiting'}
    assert runner.stage(tmp_path, op, rows, 10, network_probe=probe)[0]['request_id'] == 'waiting'
    assert len(dispatched) == 1 and json.loads(dispatched[0][1]) == probe
    assert not (tmp_path / 'stop.json').exists()


def test_stale_probe_returns_the_current_pause_without_claiming(tmp_path, monkeypatch):
    op = operation(tmp_path)
    runner.save(tmp_path / 'operation.json', op)
    dispatched = native_fixture(tmp_path, monkeypatch, {'open': OPEN}, lambda _: None)
    rows = [{'id': 'waiting', 'phase': 'incremental-page-write'}]
    probe = {'route_id': OPEN['route_id'], 'trip_id': 'older-trip',
             'operation_id': 'operator-continue', 'request_id': 'waiting'}
    with pytest.raises(runner.NetworkStagePaused) as paused:
        runner.stage(tmp_path, op, rows, 10, network_probe=probe)
    assert paused.value.circuit == OPEN
    assert dispatched == [] and not (tmp_path / 'stop.json').exists()


def test_pre_send_circuit_deferral_preserves_budget_without_consuming_ordinal(tmp_path, monkeypatch):
    import incremental_native
    import model_transport

    op = operation(tmp_path)
    runner.save(tmp_path / 'operation.json', op)
    row = {'id': 'request', 'phase': 'incremental-page-write', 'view': {}, 'schema': {}, 'image_inputs': [],
           'budget': {'fits': True, 'input_cap': 100, 'utf8_bytes': 30}, 'output_cap': 10}
    job = tmp_path / 'jobs' / 'request.json'
    runner.save(job, {'request': row})
    grant = {'task_id': 'task', 'board': op['board'], 'run_id': 7}
    reserved = []
    sending = False
    keys = []
    monkeypatch.setattr(incremental_native, 'reserve_call', lambda *args: reserved.append(args))
    monkeypatch.setattr(runner, 'checked_projection', lambda _row: {'wire': {}})

    class Caller:
        def request_scope(self, prepared):
            self.prepared = prepared
            return nullcontext()

        def __call__(self, *_):
            context = self.prepared['audit_context']
            assert context['circuit_scope'] == runner.network_scope(tmp_path, op)
            assert callable(context['persist_attempt'])
            key = runner.fingerprint([str(tmp_path), context['binding'], context['reservation'],
                                      runner.fingerprint(self.prepared['wire'])])
            keys.append(key)
            if not sending:
                report = {'state': 'request_rejected', 'source': 'transport_circuit', 'http_key': key,
                          'circuit': OPEN, 'prior_send_count': 0, 'task_id': grant['task_id'],
                          'run_id': str(grant['run_id']), 'wire_sha256': runner.fingerprint(self.prepared['wire']),
                          'http_transport': {'contract': model_transport.CONTRACT, 'physical_attempts': 0, 'sends': []}}
                path = tmp_path / 'call-failures' / (runner.fingerprint(report) + '.json')
                runner.save(path, report)
                raise TransportCircuitOpen(OPEN, http_key=key, report=str(path))
            context['persist_attempt']()
            return '{"results":{"r":{}}}', {}

    monkeypatch.setattr(runner, 'HermesCaller', Caller)
    result = runner.run_job(tmp_path, job, lambda: grant)
    marker = runner.network_defer_path(job, 7)
    assert result == {'state': 'network_deferred', 'marker_sha256': runner.sha(marker)}
    assert json.loads(marker.read_bytes())['http_key'] == keys[0]
    assert reserved == [(tmp_path, row, 1, grant)]
    assert not list(job.parent.glob('request.attempt-*.json'))
    assert not list(job.parent.glob('request.audit-*.json'))

    task = SimpleNamespace(id='task')
    stopped = SimpleNamespace(id=7, ended_at=100, outcome='gave_up',
                              error=runner.NETWORK_BLOCK_REASON + runner.sha(marker),
                              metadata={'trigger_outcome': 'network_admission_deferred'})
    state = {keys[0]: 1}
    monkeypatch.setattr(model_transport, 'network_send_count', lambda key: state[key])
    monkeypatch.setattr(model_transport, 'circuit_status', lambda _scope: OPEN)
    with pytest.raises(ValueError, match='zero HTTP sends'):
        runner.validate_network_deferral(tmp_path, job, task, stopped)
    state[keys[0]] = 0
    assert runner.validate_network_deferral(tmp_path, job, task, stopped)[0] == marker

    kb = SimpleNamespace(get_task=lambda *_args: task, list_runs=lambda *_args, **_options: [stopped])
    kc = SimpleNamespace(connect=lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runner, 'runtime', lambda: (kb, kc, None))
    monkeypatch.setattr(incremental_native, 'service', lambda *_args: SimpleNamespace(status=lambda _run_id: {
        'reservations': {'original-budget': {'state': 'reserved', 'phase': 'writing', 'max_calls': 1,
            'max_input_tokens': 30,
            'max_output_tokens': 10, 'request_fingerprint': runner.fingerprint({'request': 'request', 'ordinal': 1})}},
    }))
    monkeypatch.setattr(incremental_native, 'seal_response', lambda *_args: {'held': {}, 'sealed': ['r']})
    sending = True
    resumed_grant = {**grant, 'run_id': 8}
    assert runner.run_job(tmp_path, job, lambda: resumed_grant)['state'] == 'settled'
    assert reserved == [(tmp_path, row, 1, grant)]
    admitted = json.loads(job.with_name('request.attempt-1.json').read_bytes())
    assert admitted['grant'] == resumed_grant and keys[0] != keys[1]
