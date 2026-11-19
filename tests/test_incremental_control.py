"""Control intent survives crashes; recovery cannot retire live native work."""
import pytest

from hermes_source_units import ContractError
from hermes_source_units import incremental_control as module
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.source_units import _json_bytes, _load_json, _sha, _write_atomic
from hermes_source_units.validation import fingerprint


def control_fixture(tmp_path):
    control = IncrementalControl(tmp_path, 'isolated-control')
    control.initialize('operator', ['mapping'])
    return control


def test_writing_repair_names_held_plan_and_requires_a_new_pause(tmp_path):
    control = IncrementalControl(tmp_path, 'writing-repair')
    control.initialize('operator', ['writing'])
    root = control.root.parent / 'rounds/round-1'
    held = fingerprint('held plan')
    accepted = fingerprint('accepted plan')
    _write_atomic(control.root.parent / 'run.json', _json_bytes(
        {'active_round_id': 'round-1', 'source_workflow_id': 'isolated-source'}))
    _write_atomic(root / 'page-changes.json', _json_bytes(
        [{'change_id': key, 'action': 'create'} for key in (held, accepted)]))
    original = _json_bytes({'written': [{'example': 'accepted'}], 'held': [{'change_id': held}]})
    path = root / 'writing-disposition.json'
    _write_atomic(path, original)
    scope = {'scope_kind': 'round', 'scope_id': 'round-1'}
    paused = control.checkpoint(scope, 'writing', {'writing_disposition_sha256': _sha(original)})
    request = {'action': 'continue', 'actor': 'operator', 'operation_id': 'repair-held',
               'expected_revision': paused['revision'], 'boundary_fingerprint': fingerprint(paused['boundary'])}
    with pytest.raises(ContractError, match='only select held'):
        control.control({**request, 'repair_writing': [accepted]})
    active = control.control({**request, 'repair_writing': [held]})
    assert active['state'] == 'active'
    assert active['writing_repairs']['round-1']['plan_ids'] == [held]
    assert path.read_bytes() == original
    revised = control.checkpoint(scope, 'writing', {'writing_disposition_sha256': fingerprint('revised')})
    assert revised['state'] == 'paused' and revised['boundary'] != paused['boundary']


def test_stopped_writing_rejects_plain_continue_and_binds_explicit_continuation(tmp_path):
    from hermes_source_units.incremental_sources import pinned

    control = IncrementalControl(tmp_path, 'writing-resume')
    control.initialize('operator', ['writing'])
    root = control.root.parent
    _write_atomic(root / 'run.json', _json_bytes({'active_round_id': 'round-1', 'state': 'active', 'revision': 10,
                                               'source_workflow_id': 'isolated-source'}))
    head = control.status()
    repair = {'operation_id': 'old-writer', 'plan_ids': [fingerprint('plan')], 'prior_disposition_sha256': 'saved'}
    head['writing_repairs'] = {'round-1': repair}
    _write_atomic(control.path, _json_bytes(head))
    paused = control.control({'action': 'pause', 'actor': 'operator', 'operation_id': 'stop', 'expected_revision': 1})
    writer = root / 'native/round/round-1/writing/recoveries/old-writer'
    stop, operation = writer / 'stop.json', writer / 'operation.json'
    _write_atomic(stop, _json_bytes({'stopped': True}))
    _write_atomic(operation, _json_bytes({'bound': 'old-writer'}))
    request = {'action': 'continue', 'actor': 'operator', 'operation_id': 'next',
               'expected_revision': paused['revision'], 'boundary_fingerprint': fingerprint(paused['boundary'])}
    with pytest.raises(ContractError, match='requires recover-writing'):
        control.control(request)
    path = root / 'rounds/round-1/writing-continuations/next.json'
    _write_atomic(path, _json_bytes({
        'contract': 'incremental-writing-continuation/v1', 'round_id': 'round-1', 'run_id': control.run_id,
        'actor': 'operator', 'operation_id': 'next', 'control_revision': paused['revision'], 'run_revision': 10,
        'boundary_fingerprint': request['boundary_fingerprint'], 'original_authorization': repair,
        'previous_operation': pinned(tmp_path, operation.relative_to(tmp_path).as_posix()),
        'stop': pinned(tmp_path, stop.relative_to(tmp_path).as_posix()),
    }))
    relation = pinned(tmp_path, path.relative_to(tmp_path).as_posix())
    active = control.control({**request, 'writing_continuation': relation})
    assert active['state'] == 'active'
    assert active['writing_continuations']['round-1']['manifest'] == relation
    assert active['writing_repairs']['round-1'] == repair


def test_parallel_source_admission_waits_for_short_control_write(tmp_path, monkeypatch):
    from threading import Event, Thread, current_thread

    control = control_fixture(tmp_path)
    writing, release_write, release_workers = Event(), Event(), Event()
    entered_a, entered_b, finished_b = Event(), Event(), Event()
    failures = []
    original = module._write_atomic
    gated = False

    def write(path, data):
        nonlocal gated
        if path == control.path and current_thread().name == 'source-a' and not gated:
            gated = True
            writing.set()
            assert release_write.wait(5), 'test did not release the first admission write'
        return original(path, data)

    monkeypatch.setattr(module, '_write_atomic', write)

    def source(identity, entered, finished=None):
        try:
            with control.executing({'scope_kind': 'source', 'scope_id': identity}, 'mapping', identity):
                entered.set()
                assert release_workers.wait(5), 'test did not release admitted workers'
        except BaseException as error:
            failures.append(error)
        finally:
            if finished is not None:
                finished.set()

    first = Thread(target=source, args=('source-a', entered_a), name='source-a')
    second = Thread(target=source, args=('source-b', entered_b, finished_b), name='source-b')
    first.start()
    try:
        assert writing.wait(5)
        second.start()
        # The second source must wait rather than fail at the zero-timeout lock.
        assert not finished_b.wait(.1)
        release_write.set()
        assert entered_a.wait(5) and entered_b.wait(5)
        assert {scope['scope_id'] for scope in control.status()['active_scopes'].values()} == {'source-a', 'source-b'}
    finally:
        release_write.set()
        release_workers.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert control.status()['active_scopes'] == {}


def test_prepared_pause_revokes_admission_before_head_replacement(tmp_path, monkeypatch):
    control = control_fixture(tmp_path)
    write = module._write_atomic

    def fail_head(path, data):
        if path == control.path:
            raise OSError('isolated crash before replacing control head')
        return write(path, data)

    request = {'run_id': control.run_id, 'actor': 'operator', 'operation_id': 'pause-one',
               'expected_revision': 1, 'action': 'pause'}
    monkeypatch.setattr(module, '_write_atomic', fail_head)
    with pytest.raises(OSError):
        control.control(request)
    assert _load_json(control.path)['state'] == 'active'
    assert control.status()['state'] == 'paused'
    with pytest.raises(ContractError, match='holds this scope'):
        control.check_admission({'scope_kind': 'source', 'scope_id': 'source-a'}, 'mapping', 'input-a')
    monkeypatch.setattr(module, '_write_atomic', write)
    resumed_reader = IncrementalControl(tmp_path, control.run_id)
    assert resumed_reader.initialize('operator', ['mapping'])['state'] == 'paused'
    assert resumed_reader.control(request)['state'] == 'paused'
    assert _load_json(control.root / 'operations/pause-one.json')['commit_state'] == 'committed'


def test_dead_executor_reconciliation_preserves_pause_and_retains_evidence(tmp_path, monkeypatch):
    control = control_fixture(tmp_path)
    head = control.status()
    head.update(state='pausing', active_scopes={'original': {
        'scope_kind': 'source', 'scope_id': 'source-a', 'owner': {'pid': 424242}}})
    module._write_atomic(control.path, _json_bytes(head))
    monkeypatch.setattr(control, '_owner_alive', lambda owner: False)
    monkeypatch.setattr(control, '_scope_has_native_consumers', lambda scope: False)
    reconciled = control.reconcile_executors()
    assert reconciled['state'] == 'pausing'
    assert reconciled['active_scopes'] == {}
    assert len(list((control.root / 'executor-reconciliations').glob('*.json'))) == 1
    assert control.checkpoint({'scope_kind': 'run', 'scope_id': control.run_id}, 'mapping', {})['state'] == 'paused'


def test_original_native_work_prevents_executor_retirement(tmp_path, monkeypatch):
    control = control_fixture(tmp_path)
    head = control.status()
    head['active_scopes'] = {'original': {'scope_kind': 'source', 'scope_id': 'source-a', 'owner': {'pid': 424242}}}
    module._write_atomic(control.path, _json_bytes(head))
    monkeypatch.setattr(control, '_owner_alive', lambda owner: False)
    monkeypatch.setattr(control, '_scope_has_native_consumers', lambda scope: True)
    with pytest.raises(ContractError, match='original tasks'):
        control.reconcile_executors()
    assert control.status()['active_scopes'] == head['active_scopes']


def test_unknown_checkpoints_and_cross_run_admission_are_rejected(tmp_path):
    control = IncrementalControl(tmp_path, 'isolated-control')
    with pytest.raises(ContractError, match='unknown incremental checkpoint'):
        control.initialize('operator', ['source-seal'])
    control.initialize('operator', [])
    with pytest.raises(ContractError, match='another run or actor'):
        control.check_admission({'run_id': 'other', 'scope_kind': 'run', 'scope_id': 'other'}, 'prepare', 'input-a')


@pytest.mark.parametrize('state,native_status,body_changed,accepted', [
    ('paused', 'running', False, False),
    ('pausing', 'ready', False, False),
    ('pausing', 'running', True, False),
    ('pausing', 'running', False, True),
])
def test_source_begin_respects_pause_and_checks_exact_existing_claim(
        tmp_path, monkeypatch, state, native_status, body_changed, accepted):
    import hashlib
    import json
    from pathlib import Path
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-controlled-ingest/lib'))
    import ingest_kanban as kanban

    source = tmp_path / '10_Raw/a.pdf'
    source.parent.mkdir()
    source.write_bytes(b'isolated fixture source')
    node_name = 'source-prepare:' + module.fingerprint({
        'path': '10_Raw/a.pdf', 'content_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})[:16]
    node = SimpleNamespace(name=node_name, kind='source-prepare', input_fingerprint='source-input')
    workflow = {'workflow_id': 'ingest-fixture', 'cancel_requested': False, 'batch_id': None,
                'scope': {'workflow_mode': 'incremental_v1', 'incremental_run_id': 'fixture',
                          'source_paths': ['10_Raw/a.pdf']},
                'kanban': {'board_id': 'fixture-board', 'task_map': [{
                    'node': node_name, 'task_id': 'native-a',
                    'idempotency_key': 'ingest:ingest-fixture:source-prepare:source-input'}]},
                'template_pins': [{'kind': 'source-prepare', 'template_hash': 'pinned-template'}]}
    body = {'workflow_id': workflow['workflow_id'], 'node': node_name,
            'input_fingerprint': node.input_fingerprint, 'vault': str(tmp_path)}
    if body_changed:
        body['node'] = 'source-prepare:unrelated'
    native = {'id': 'native-a', 'status': native_status, 'body': json.dumps(body)}
    adapter = kanban.IngestKanbanAdapter.__new__(kanban.IngestKanbanAdapter)
    adapter.enable_workers = True
    adapter.workflow = SimpleNamespace(vault=tmp_path, status=lambda _: workflow,
                                       pinned_templates=lambda _: None)
    adapter.kanban = SimpleNamespace(task_snapshot_fresh=lambda *_: {'task': native})
    monkeypatch.setattr(kanban, 'desired_graph', lambda *_: [node])
    monkeypatch.setattr(adapter, '_execution_blocked', lambda *_: False)
    monkeypatch.setattr(module, 'IncrementalControl', lambda *_: SimpleNamespace(status=lambda: {'state': state}))
    request = {'workflow_id': workflow['workflow_id'], 'node': node_name, 'task_id': 'native-a'}
    if accepted:
        assert adapter._pre_worker(request, beginning=True)[2]['path'] == '10_Raw/a.pdf'
    else:
        with pytest.raises(ContractError, match='WORKFLOW_PAUSED'):
            adapter._pre_worker(request, beginning=True)
