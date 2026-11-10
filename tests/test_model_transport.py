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

    def create(name, guard=lambda: None):
        return model.Transport({
            'transport_db': tmp_path / 'transport.sqlite', 'audit_root': tmp_path,
            'binding': {'task_id': name}, 'reservation': name, 'wire_sha256': name,
            'control_check': guard, 'phase': 'map', 'transport_profile': 'paced',
        }, 'session-' + name, 'model', input_size=10, output_cap=20)
    return now, create


def send(t, status, *, retry_after='1', sse=None):
    headers = {'x-request-id': 'request_1', 'Retry-After': retry_after, 'Set-Cookie': 'private'}
    if sse:
        headers['content-type'] = 'text/event-stream'
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, stream=httpx.ByteStream(sse or b'{}'), headers=headers)
    )) as client:
        t.attach(SimpleNamespace(_client=client, max_retries=0))
        return client.post('https://offline.invalid/responses', json={'model': 'model'},
                           headers={'Authorization': 'Bearer private'})


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
