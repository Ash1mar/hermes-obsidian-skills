"""Control intent survives crashes; recovery cannot retire live native work."""
import pytest

from hermes_source_units import ContractError
from hermes_source_units import incremental_control as module
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.source_units import _json_bytes, _load_json


def control_fixture(tmp_path):
    control = IncrementalControl(tmp_path, 'isolated-control')
    control.initialize('operator', ['mapping'])
    return control


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
