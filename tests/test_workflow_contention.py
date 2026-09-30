"""Real lock contention: temporary waits must not bypass worker authority."""
from contextlib import contextmanager
import json
import threading
import time

import pytest

from test_ingest_worker_recovery import setup_worker
from hermes_source_units import ContractError
from hermes_source_units.file_locks import exclusive_lock
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


@contextmanager
def held_lock(path, *, change=None):
    ready, release = threading.Event(), threading.Event()
    errors = []
    def holder():
        try:
            with exclusive_lock(path):
                if change:
                    change()
                ready.set()
                assert release.wait(5), 'test did not release holder'
        except BaseException as exc:
            errors.append(exc)
            ready.set()
    thread = threading.Thread(target=holder)
    thread.start()
    assert ready.wait(5)
    try:
        assert not errors, errors
        yield release
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors, errors


def bound_worker(tmp_path):
    vault, adapter, value, request = setup_worker(tmp_path)
    request['template_hash'] = adapter.worker_begin(request)['template_hash']
    return vault, adapter, value, request


def test_worker_write_waits_for_transient_contention(tmp_path):
    vault, adapter, value, request = bound_worker(tmp_path)
    wrote = vault / '_system/reports/guarded-result.json'
    with held_lock(adapter.workflow._lock(value['workflow_id'])) as release:
        timer = threading.Timer(.2, release.set)
        timer.start()
        try:
            with worker_binding(request), workflow_write_guard(vault, actor='agent'):
                wrote.write_text('{}')
        finally:
            timer.cancel()
            timer.join()
    assert wrote.exists()
    assert not list((vault/'_system/ledgers/ingest-workflows'/value['workflow_id']/'reports').glob('failed-*'))


def test_lock_wait_has_a_deadline_and_keeps_holder_marker(tmp_path):
    lock = tmp_path/'wait.lock'
    with held_lock(lock):
        before = lock.read_bytes()
        start = time.monotonic()
        with pytest.raises(ContractError, match='LOCK_TIMEOUT'):
            with exclusive_lock(lock, timeout=.15):
                pytest.fail('contender acquired a held lock')
        assert .1 <= time.monotonic()-start < 2
        assert lock.read_bytes() == before


@pytest.mark.parametrize('change,code', [('cancel','WORKFLOW_STOPPED'),
    ('card','STALE_INPUT'), ('hash','TEMPLATE_CHANGED'), ('template','TEMPLATE_CHANGED')])
def test_wait_rechecks_live_binding_before_any_write(tmp_path, monkeypatch, change, code):
    vault, adapter, value, request = bound_worker(tmp_path)
    import hermes_source_units.file_locks as locks
    attempted = threading.Event()
    original_sleep = locks.time.sleep
    def sleep(seconds):
        attempted.set()
        original_sleep(seconds)
    monkeypatch.setattr(locks.time, 'sleep', sleep)
    errors = []
    def contender():
        try:
            with worker_binding(request), workflow_write_guard(vault, actor='agent'):
                (vault/'_system/reports/forbidden.json').write_text('{}')
        except ContractError as exc:
            errors.append(exc.code)
    with held_lock(adapter.workflow._lock(value['workflow_id'])):
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempted.wait(2), 'worker did not wait on contention'
        # Simulate the atomic commit made by the current lock owner, not a
        # concurrent unlocked writer. Keep the lock held after its commit.
        current = adapter.workflow.status(value['workflow_id'])
        if change == 'cancel':
            current.update(cancel_requested=True, state='cancelled',
                           resume_stage=current['current_stage'])
        elif change == 'card':
            current['kanban']['task_map'][0]['task_id'] = 'superseded'
        elif change == 'hash':
            current['template_pins'][0]['template_hash'] = 'sha256:'+'0'*64
        else:
            path = vault/current['template_pins'][0]['path']
            path.write_text('changed template')
        current['revision'] += 1
        adapter.workflow._write(current)
        thread.join(2)
        assert not thread.is_alive(), 'invalid binding waited for lock release'
    assert errors == [code]
    assert not (vault/'_system/reports/forbidden.json').exists()
