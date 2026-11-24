"""Compare managed Hermes accounting with the historical offline SQLite reader."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sys

import pytest


LIB = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/lib'
sys.path.insert(0, str(LIB))

from incremental_closure import session_row
from fixed_semantic_recovery import transcript_reply
from hermes_source_units.validation import fingerprint
from session_usage_reader import SessionStorageUnavailable, active_session_records
from workflow_metrics import usage_for


def create_sessions(profile, sessions, *, exchanges=None):
    assembly = pytest.importorskip('persistence_assembly')
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence.conversation.usage import TokenCounts, UsageDelta

    profile.mkdir()
    with assembly.open_persistence(assembly.profile_scope(home=profile), eager=True) as store:
        for sid, usage in sessions.items():
            store.sessions.create(NewSession(session_id=sid, source='kanban'))
            if exchange := (exchanges or {}).get(sid):
                for role, content in zip(('user', 'assistant'), exchange, strict=True):
                    store.messages.append(sid, NewMessage(role=role, content=content))
            if usage:
                store.usage.queue_usage(sid, UsageDelta(tokens=TokenCounts(**usage), api_call_count=1,
                                                        source='kanban'))
            store.sessions.end(sid, 'fixture-ended')
        assert store.usage.flush_usage()


def test_profile_session_fields_match_explicit_offline_database(tmp_path, monkeypatch):
    home, other = tmp_path / 'requested', tmp_path / 'wrong'
    known, zero, missing = 'session-20261124-29daf2', 'session-20261124-7dea53', 'session-20261124-absent'
    tokens = {'input_tokens': 31, 'output_tokens': 7, 'cache_read_tokens': 13, 'reasoning_tokens': 3}
    user, answer = 'Exact frozen Map input', '{"map":"fixture response"}'
    create_sessions(home, {known: tokens, zero: None}, exchanges={known: (user, answer)})
    create_sessions(other, {known + '-other': {'input_tokens': 999}})
    monkeypatch.setenv('HERMES_HOME', str(home))
    database = home / 'state.db'
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    calls = [{'model': {'session_id': sid}} for sid in (known, zero, missing, known)]

    with active_session_records() as lookup:
        returned = session_row(lookup, known)
        assert (returned['source'], returned['calls'], returned['input_tokens'], returned['output_tokens']) == (
            'kanban', 1, 31, 7)
        assert returned['ended_at_us'] > 0
        assert session_row(lookup, known[:-1]) is None
        actual_zero = session_row(lookup, zero)
        assert actual_zero['calls'] == actual_zero['input_tokens'] == actual_zero['output_tokens'] == 0
        assert actual_zero['ended_at_us'] > 0
        assert lookup(missing) is None
        bound = usage_for(calls, lookup=lookup)
    online = usage_for(calls, active=True)
    assert online == bound == usage_for(calls, database)
    assert online['unique_sessions'] == 3
    assert online['known_totals'] == {'input': 31, 'output': 7, 'cache_read_subset': 13, 'reasoning': 3,
                                      'api_calls': 1}
    assert set(online['unknown_samples'].values()) == {1}
    proof = {'user_sha256': fingerprint(user), 'response_sha256': fingerprint(answer),
             'model': {'origin': 'hermes_native_model', 'session_id': known, 'response_sha256': fingerprint(answer)}}
    assert transcript_reply(proof, {'user': user}) == transcript_reply(proof, {'user': user}, home=home) == answer
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    monkeypatch.setenv('HERMES_HOME', str(other))
    assert transcript_reply(proof, {'user': user}) is None
    with active_session_records() as lookup:
        assert lookup(known) is None and lookup(zero) is None
        assert lookup(known + '-other')['input_tokens'] == 999
    assert set(usage_for(calls, active=True)['unknown_samples'].values()) == {3}


def test_missing_profile_store_is_never_created(tmp_path, monkeypatch):
    pytest.importorskip('persistence_assembly')
    home = tmp_path / 'empty-profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    with pytest.raises(SessionStorageUnavailable), active_session_records():
        pytest.fail('the missing store was opened')
    assert not (home / 'state.db').exists()


def test_contended_store_keeps_usage_unknown_and_strict_recheck_fails(monkeypatch):
    assembly = pytest.importorskip('persistence_assembly')
    from persistence import StoreContention

    @contextmanager
    def contend(_profile):
        raise StoreContention('fixture storage read contention')
        yield

    monkeypatch.setattr(assembly, 'open_persistence_reader', contend)
    calls = [{'model': {'session_id': 'fixture-session'}}]
    assert usage_for(calls, active=True) == usage_for(calls)
    with pytest.raises(SessionStorageUnavailable, match='fixture storage read contention'), active_session_records():
        pytest.fail('a strict caller received a reader for the contended store')
