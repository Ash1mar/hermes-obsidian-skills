"""No broad replay lifetime silently replaces per-response current checks."""
import os
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json
from ingest_graph_reuse import graph_scope,derive
from test_graph_and_proof_reuse import graph_fixture
import fixed_semantic_recovery as recovery


def test_historical_response_exit_observes_same_mtime_dependency_rewrite(tmp_path,monkeypatch):
    service,w,task,_=graph_fixture(tmp_path);checks=[]
    initial=_load_json(task)
    def check(_):
        checks.append(1)
        actual=derive(service,w,lambda:[_load_json(task)])
        if actual!=[initial]:raise ContractError('STALE_INPUT','$','dependency changed')
    def proof(*args,**kwargs):
        stamp=task.stat().st_mtime_ns;task.write_text('{"value":"changed"}');os.utime(task,ns=(stamp,stamp))
        return ('saved response',{})
    monkeypatch.setattr(recovery,'permitted_tasks',lambda *_a,**_k:{})
    monkeypatch.setattr(recovery,'native_proof',proof)
    with graph_scope(),pytest.raises(ContractError,match='dependency changed'):
        recovery.native_reply(NS(worker_check=check),{},None,{}, {},[])
    assert len(checks)==2


def test_adjacent_entry_flag_keeps_exit_check_and_next_authentication(tmp_path,monkeypatch):
    checks=[];cancelled=[False]
    def check(_):
        checks.append(1)
        if cancelled[0]:raise ContractError('WORKFLOW_STOPPED','$','cancelled without revision change')
    monkeypatch.setattr(recovery,'permitted_tasks',lambda *_a,**_k:{})
    monkeypatch.setattr(recovery,'native_proof',lambda *_a,**_k:('saved response',{}))
    a=NS(worker_check=check)
    assert recovery.native_reply(a,{},None,{}, {},[],entry_checked=True)[0]=='saved response'
    cancelled[0]=True
    with pytest.raises(ContractError,match='cancelled without revision change'):
        recovery.native_reply(a,{},None,{}, {},[],entry_checked=True)
    assert len(checks)==2
