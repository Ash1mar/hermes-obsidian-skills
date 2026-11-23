"""Isolated HTTP attempts using the same hooks as native workers."""
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
import model_transport as model


@pytest.fixture
def local(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(model.time, 'time', lambda: now[0])
    monkeypatch.setattr(model.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(model.random, 'uniform', lambda *_: 1)

    def create(name, guard=lambda: None, *, scope=None, probe=None, persist=None, request_id=None):
        context = {
            'transport_db': tmp_path / 'transport.sqlite', 'audit_root': tmp_path,
            'binding': {'task_id': name}, 'reservation': {'request_sha256': request_id or name}, 'wire_sha256': name,
            'control_check': guard, 'phase': 'map', 'transport_profile': 'paced',
        }
        if scope:
            context.update(circuit_scope=scope, circuit_probe=probe, persist_attempt=persist)
        return model.Transport(context, 'session-' + name, 'model', input_size=10, output_cap=20)
    return now, create


def send(t, status, *, retry_after='1', sse=None, fault=None, credential='Bearer private'):
    headers = {'x-request-id': 'request_1', 'Retry-After': retry_after, 'Set-Cookie': 'private'}
    if sse:
        headers['content-type'] = 'text/event-stream'
    def response(_):
        if fault is not None:
            raise fault
        return httpx.Response(status, stream=httpx.ByteStream(sse or b'{}'), headers=headers)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        t.attach(SimpleNamespace(_client=client, max_retries=0))
        return client.post('https://offline.invalid/responses', json={'model': 'model'},
                           headers={'Authorization': credential})


def failed(state='request_rejected', code='rate_limit_exceeded'):
    return {'state': state, 'error': {'code': code}}


def test_shared_cooldown_and_durable_retry_budget(local):
    now, create = local
    a = create('a')
    send(a, 429, retry_after='5')
    assert a.finish(outcome=failed())
    b = create('b')
    send(b, 200)
    assert b.row['sent_at'] == 1005 and b.row['sent_previous_10_seconds'] == 1
    assert 'set-cookie' not in b.row['response_headers'] and b.row['route']['account'] != 'Bearer private'
    b.finish(response=SimpleNamespace())
    a = create('a')
    send(a, 429)
    assert a.row['attempt'] == 2 and a.row['sent_at'] == 1006 and a.finish(outcome=failed())
    a = create('a')
    send(a, 429)
    assert not a.finish(outcome=failed()) and len(a.refs) == 3
    with pytest.raises(model.AdmissionStopped):
        send(create('a'), 200)


def test_retry_after_over_bound_holds_and_wait_observes_cancel(local):
    now, create = local
    a = create('a')
    send(a, 429, retry_after='121')
    assert not a.finish(outcome=failed())
    def stop():
        if now[0] >= 1001:
            raise RuntimeError('cancelled')
    with pytest.raises(model.AdmissionStopped, match='cancelled'):
        send(create('b', stop), 200)
    assert now[0] == 1001


def test_overloaded_partial_response_keeps_identity_and_never_resends(local):
    _, create = local
    a = create('a')
    send(a, 200, sse=b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        b'data: {"type":"error","code":"server_is_overloaded"}\n\n')
    assert a.row['partial_output'] and a.row['response_id'] == 'resp_1'
    assert a.retrieve(failed('completion_unknown', 'server_is_overloaded')) is None
    assert not a.finish(outcome=failed('completion_unknown', 'server_is_overloaded'))
    with pytest.raises(model.AdmissionStopped):
        send(create('a'), 200)
    b = create('b')
    send(b, 200)
    assert b.row['sent_at'] == 1001  # Retry-After applies to the route, not permission to resend.


def test_unbudgeted_sdk_send_and_crash_pending_are_fenced(local):
    _, create = local
    a = create('a')
    send(a, 200)
    with pytest.raises(model.AdmissionStopped, match='unbudgeted'):
        send(a, 200)
    assert a.row['sdk_retries_blocked'] == 1
    with pytest.raises(model.AdmissionStopped, match='unknown completion'):
        send(create('a'), 200)


def unknown_connection():
    return {'state': 'completion_unknown', 'error': {'class': 'APIConnectionError', 'message': 'Connection error.'}}


def first_event_timeout():
    return {'state': 'completion_unknown', 'error': {'class': 'TimeoutError',
        'message': 'Codex stream produced no parsed stream event within 120s (TTFB threshold: 120s)'}}


def network_failure(create, name, outcome=None):
    transport = create(name, scope='workflow')
    send(transport, 200)
    assert not transport.finish(outcome=outcome or unknown_connection())
    return transport


def trip_route(create):
    for request_id in ('failed-1', 'failed-2', 'failed-3'):
        transport = network_failure(create, request_id)
    return transport.circuit, transport.journal.path


def probe_grant(circuit, operation, request):
    return {'route_id': circuit['route_id'], 'trip_id': circuit['trip_id'],
            'operation_id': operation, 'request_id': request}


def test_circuit_counts_terminal_network_failures_resets_and_blocks_before_send(local):
    _, create = local
    assert network_failure(create, 'first').circuit['consecutive_failures'] == 1
    good = create('good', scope='workflow')
    send(good, 200)
    good.finish(response=SimpleNamespace())
    assert good.circuit['consecutive_failures'] == 0
    assert network_failure(create, 'second', first_event_timeout()).circuit['consecutive_failures'] == 1
    assert network_failure(create, 'third').circuit['consecutive_failures'] == 2
    inflight = create('already-in-flight', scope='workflow')
    send(inflight, 200)
    tripping = network_failure(create, 'fourth')
    current = tripping.circuit
    assert current['state'] == 'open' and current['consecutive_failures'] == 3
    inflight.finish(response=SimpleNamespace())
    assert model.circuit_status('workflow', transport_db=tripping.journal.path) == current

    persisted = []
    blocked = create('blocked', scope='workflow', persist=lambda: persisted.append(True))
    with pytest.raises(model.TransportCircuitOpen) as detected:
        send(blocked, 200)
    assert detected.value.circuit == current and not detected.value.send_started
    assert detected.value.http_key == blocked.key and detected.value.prior_send_count == 0
    assert not persisted and not blocked.evidence()['physical_attempts']
    assert model.network_send_count(blocked.key, transport_db=blocked.journal.path) == 0
    ordinary = create('non-opted')
    send(ordinary, 200)
    ordinary.finish(response=SimpleNamespace())
    other_route = create('another-route', scope='different-workflow')
    send(other_route, 200, credential='Bearer other credentials')
    other_route.finish(response=SimpleNamespace())


def test_safe_physical_retry_counts_once_after_terminal_failure(local):
    _, create = local
    transport = create('retry', scope='workflow')
    error = httpx.ConnectError('could not establish connection')
    for attempt in range(3):
        with pytest.raises(httpx.ConnectError):
            send(transport, 200, fault=error)
        assert transport.finish(error=error, outcome=unknown_connection()) == (attempt < 2)
        assert transport.circuit['consecutive_failures'] == (1 if attempt == 2 else 0)
    assert model.network_send_count(transport.key, transport_db=transport.journal.path) == 3


def test_single_bound_probe_fences_replay_and_resets_only_on_success(local):
    _, create = local
    late = create('old-generation', scope='workflow')
    send(late, 200)
    original, db = trip_route(create)
    good = probe_grant(original, 'continue-1', 'probe-1')
    saved = []
    probe = create('probe-1', scope='workflow', probe=good, persist=lambda: saved.append(True))
    send(probe, 200)
    assert saved == [True]
    status = model.circuit_status('workflow', transport_db=db)
    assert (status['probe_state'], status['probe_operation_id'], status['probe_request_id'], status['probe_http_key']) == (
        'started', 'continue-1', 'probe-1', probe.key)
    competing = create('probe-race', scope='workflow', probe=good, request_id='probe-1')
    with pytest.raises(model.TransportCircuitOpen):
        send(competing, 200)
    assert model.network_send_count(competing.key, transport_db=db) == 0
    assert not probe.finish(outcome=unknown_connection())
    current = model.circuit_status('workflow', transport_db=db)
    assert current['trip_id'] != original['trip_id'] and current['state'] == 'open'
    for operation_id, request_id in [('continue-1', 'other'), ('continue-reused-request', 'probe-1')]:
        grant = probe_grant(current, operation_id, request_id)
        with pytest.raises(model.TransportCircuitOpen):
            send(create(operation_id, scope='workflow', probe=grant, request_id=request_id), 200)
    next_probe = create('probe-2', scope='workflow', probe=probe_grant(current, 'continue-2', 'probe-2'))
    send(next_probe, 200)
    next_probe.finish(response=SimpleNamespace())
    late.finish(outcome=unknown_connection())
    assert model.circuit_status('workflow', transport_db=db)['state'] == 'closed'
    assert model.circuit_status('workflow', transport_db=db)['consecutive_failures'] == 0


def test_retiring_drained_probe_preserves_unknown_send_and_fences_its_original_scope(local):
    _, create = local
    original, db = trip_route(create)
    grant = probe_grant(original, 'continue-1', 'probe-1')
    probe = create('probe-1', scope='workflow', probe=grant)
    send(probe, 200)
    replacement = model.retire_circuit_probe('workflow', **grant, transport_db=db)
    assert replacement['state'] == 'open' and replacement['trip_id'] != original['trip_id']
    assert model.retire_circuit_probe('workflow', **grant, transport_db=db) == replacement
    assert model.network_send_count(probe.key, transport_db=db) == 1
    assert probe.journal.history(probe.key)[0]['ended'] is None
    probe.finish(response=SimpleNamespace())  # An old generation cannot close the replacement trip.
    assert model.circuit_status('workflow', transport_db=db) == replacement
    with pytest.raises(model.TransportCircuitOpen):
        replay = create('replay', scope='workflow', probe=probe_grant(replacement, 'continue-2', 'probe-1'), request_id='probe-1')
        send(replay, 200)
