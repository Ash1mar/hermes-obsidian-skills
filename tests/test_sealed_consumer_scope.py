"""A Build-only registry must not prevent ordinary later-stage workers."""
import json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units import evidence_snapshot as sealed


@pytest.mark.parametrize('node',['global-reduce','vault-finalize-plan','checkpoint-2-validate','release-apply','provider-sync'])
def test_other_nodes_ignore_retained_build_registry_and_clear_inherited_binding(tmp_path,monkeypatch,node):
    directory=tmp_path/sealed.ROOT/'w';directory.mkdir(parents=True)
    (directory/'state.json').write_text(json.dumps({'contract':sealed.CONTRACT,'state':'revoked'}))
    monkeypatch.setenv('HERMES_KANBAN_TASK','native-card')
    monkeypatch.setenv('HERMES_EVIDENCE_WORKFLOW','old-inherited-binding')
    monkeypatch.setattr(sealed,'restrict_consumer',lambda:pytest.fail('unrelated node must keep ordinary path'))
    sealed.configure_consumer('w',node)
    with sealed.snapshot_scope(NS(vault=tmp_path)) as view:assert view is None


def test_bound_build_still_fails_closed_on_revocation(tmp_path,monkeypatch):
    directory=tmp_path/sealed.ROOT/'w';directory.mkdir(parents=True)
    (directory/'lease.lock').touch()
    (directory/'state.json').write_text(json.dumps({'contract':sealed.CONTRACT,'state':'revoked'}))
    monkeypatch.setenv('HERMES_KANBAN_TASK','native-card')
    monkeypatch.setattr(sealed,'restrict_consumer',lambda:None)
    sealed.configure_consumer('w','build-finalize')
    with pytest.raises(ContractError):
        with sealed.snapshot_scope(NS(vault=tmp_path)):pytest.fail('revoked Build must not enter')
