"""Finite attempts survive worker/reconciler restarts and preserve task scope."""
from types import SimpleNamespace
from pathlib import Path
import json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units import ContractError
from pass_local_review import reserve_budget,finish_budget


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr('orchestration.configuration',lambda:{'pass_failure_policy':'once_then_isolate'})
    a=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'scope':{'execution_mode':'auto_full'}}))
    binding={'workflow_id':'ingest-finite','node':'pass-slice:one','task_id':'native-1'}
    view={'tasks':[{'task':'t1','materials':[]},{'task':'t2','materials':[]}]}
    return a,binding,view,{'t1':'work-1','t2':'work-2'}


def test_restart_and_regrouping_cannot_mint_a_third_attempt(tmp_path,monkeypatch):
    a,b,v,ids=setup(tmp_path,monkeypatch)
    first=reserve_budget(a,b,'candidate',v,v['tasks'],ids)
    finish_budget(first,'first.json')
    b={**b,'task_id':'native-restarted'}
    second=reserve_budget(a,b,'candidate',v,[v['tasks'][1]],ids)
    finish_budget(second,'second.json')
    with pytest.raises(ContractError,match='allowance exhausted'):
        reserve_budget(a,b,'candidate',v,[v['tasks'][1]],ids)
    ledger=json.loads(first[0].read_text())
    assert sorted(len(r['attempts']) for r in ledger['tasks'].values())==[1,2]
    assert all(r['state']=='returned' for t in ledger['tasks'].values() for r in t['attempts'])


def test_unknown_call_does_not_become_a_retry_or_exclusion(tmp_path,monkeypatch):
    a,b,v,ids=setup(tmp_path,monkeypatch)
    reserve_budget(a,b,'citation',v,v['tasks'],ids)
    with pytest.raises(ContractError,match='completion unknown'):
        reserve_budget(a,{**b,'task_id':'new-worker'},'citation',v,v['tasks'],ids)


def test_adopted_native_attempt_leaves_exactly_one_call(tmp_path,monkeypatch):
    a,b,v,ids=setup(tmp_path,monkeypatch)
    grant={'original_call_ref':'native/original.json'}
    token=reserve_budget(a,b,'candidate',v,v['tasks'],ids,grant)
    finish_budget(token,'native/correction.json')
    with pytest.raises(ContractError,match='allowance exhausted'):
        reserve_budget(a,b,'candidate',v,v['tasks'],ids,grant)
    assert all(len(r['attempts'])==2 for r in json.loads(token[0].read_text())['tasks'].values())


def test_staged_workflow_does_not_adopt_automatic_policy(tmp_path,monkeypatch):
    a,b,v,ids=setup(tmp_path,monkeypatch)
    a.workflow.status=lambda _: {'scope':{'execution_mode':'canary_only'}}
    assert reserve_budget(a,b,'candidate',v,v['tasks'],ids) is None
    assert not list(tmp_path.rglob('*.json'))
