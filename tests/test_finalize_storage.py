"""Mechanical output layout does not create pages or relax source protection."""
from types import SimpleNamespace as NS
import pytest
from hermes_source_units.partial_finalize import prepare_storage
from hermes_source_units import ContractError

def test_stopped_storage_prepare_creates_only_lock_and_revision_container(tmp_path):
    w={'state':'cancelled','current_stage':'build_finalizing'}
    paths=prepare_storage(NS(vault=tmp_path),w)
    assert len(paths)==2
    assert (tmp_path/paths[0]).is_file() and (tmp_path/paths[1]).is_dir()
    assert not list((tmp_path/paths[1]).iterdir())
    assert not list(tmp_path.rglob('manifest.json')) and not list(tmp_path.rglob('*.md'))
    assert prepare_storage(NS(vault=tmp_path),w)==paths

@pytest.mark.parametrize('change',['active','worker','delegated','symlink'])
def test_storage_preparation_never_runs_as_a_live_domain_worker(tmp_path,monkeypatch,change):
    w={'state':'cancelled','current_stage':'build_finalizing'}
    if change=='active':w['state']='build_finalizing'
    elif change=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    elif change=='delegated':monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT','child')
    else:
        root=tmp_path/'_system/knowledge-builds';root.mkdir(parents=True)
        (root/'page-revisions').symlink_to(tmp_path/'outside',target_is_directory=True)
    with pytest.raises(ContractError):prepare_storage(NS(vault=tmp_path),w)
