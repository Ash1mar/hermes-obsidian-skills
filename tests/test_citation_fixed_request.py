"""Isolated dispatch interruption and final-wire checks; no real Vault/model."""
import copy
from types import SimpleNamespace
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
import hermes_source_units.citation_amendment as core
import citation_amendment_calls as calls
import fixed_semantic as semantic
from test_citation_amendment_safety import wallet


def setup(tmp_path,monkeypatch):
    wid,value=wallet(tmp_path)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID','1')
    binding={'workflow_id':wid,'node':'pass-slice:s','task_id':'native','template_hash':'a'*64}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _:{'kanban':{'board_id':'isolated'}}),
        worker_check=lambda _:None,kanban=SimpleNamespace(task_snapshot=lambda *_:{
            'task':{'worker_pid':None},'runs':[{'id':1,'ended_at':2}]}))
    view={'tasks':[{'task':'t1','materials':[],'candidate':{'revision_review':True,'sequence':1,
          'candidates':[],'inspections':[],'empty_reason':''}}],'texts':{},'headings':[]}
    from fixed_semantic_schema import slot_schema
    schema=slot_schema('citation',view['tasks'])
    prepared=semantic.prepare_request_projection('citation',view,schema,[])
    return wid,binding,adapter,view,prepared


@pytest.mark.parametrize('changed',['none','view','content','wire','schema'])
def test_dispatch_claims_exact_final_request_once(tmp_path,monkeypatch,changed):
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    reservation=core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],binding)
    if changed=='view':view['texts']['new']='changed after reservation'
    if changed=='content':p['content']='different actual content'
    if changed=='wire':p['wire']['system']='different wire'
    if changed=='schema':p['identity']['output_schema']={'changed':True}
    token=calls.CALL.set(reservation)
    try:
        if changed!='none':
            with pytest.raises(ContractError):calls.check_dispatch(adapter,binding,'citation',view,p)
        else:
            calls.check_dispatch(adapter,binding,'citation',view,p)
            with pytest.raises(ContractError,match='already claimed'):calls.check_dispatch(adapter,binding,'citation',view,p)
    finally:calls.CALL.reset(token)
    r=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts']
    assert len(r)==1 and r[0]['state']==('dispatch_claimed' if changed=='none' else 'reserved')


def test_legacy_unknown_is_rejected_before_recovery_mutates_any_state(tmp_path,monkeypatch):
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    reservation=core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],binding)
    path=reservation[0];ledger=core.pending_calls(tmp_path,wid)
    del ledger['jobs']['citation:original-task']['attempts'][0]['events']
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    _write_atomic(path,_json_bytes(ledger));before=path.read_bytes()
    rows=calls.execution_status(adapter,wid,binding['node'])
    assert rows[0]['state']=='completion_unknown' and rows[0]['charged']
    with pytest.raises(ContractError,match='execution admission unmet'):
        calls.execution_admission(adapter,wid,binding['node'])
    with pytest.raises(ContractError,match='unknown or dispatched'):
        core.continue_call(reservation,'a'*64,['1'])
    assert path.read_bytes()==before


def test_certified_untouched_reservation_continues_same_ordinal_but_claim_is_never_refunded(tmp_path,monkeypatch):
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    reservation=core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],binding)
    import citation_amendment_control as control
    monkeypatch.setattr(control,'contract_hash',lambda:'a'*64)
    core.continue_call(reservation,'a'*64,['1']);before=reservation[0].read_bytes()
    core.continue_call(reservation,'a'*64,['1']);assert reservation[0].read_bytes()==before
    replacement={**binding,'task_id':'replacement'}
    core.claim_call(reservation,p['wire'],p['identity']['output_schema'],replacement,'2')
    with pytest.raises(ContractError,match='already claimed'):
        core.claim_call(reservation,p['wire'],p['identity']['output_schema'],replacement,'2')
    with pytest.raises(ContractError,match='unknown or dispatched'):
        core.continue_call(reservation,'a'*64,['1','2'])
    records=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts']
    assert len(records)==1 and records[0]['binding']==binding
    assert calls.execution_record(records[0])['binding']==replacement


def test_append_events_retain_history_and_do_not_multiply_a_request(tmp_path,monkeypatch):
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    r=core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],binding)
    core.claim_call(r,p['wire'],p['identity']['output_schema'],binding,'1')
    old=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts'][0]
    core.update_call(r,session_id='native-session');before=r[0].read_bytes()
    core.update_call(r,session_id='native-session');assert r[0].read_bytes()==before
    record=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts'][0]
    assert record['events'][-1]['previous_sha256']==fingerprint(old)
    with pytest.raises(ContractError,match='immutable'):core.update_call(r,wire={'user':'different'})


def test_dispatch_uses_the_same_projection_instead_of_reconstructing(tmp_path,monkeypatch):
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    monkeypatch.setattr('reduce_coverage.current',lambda *_:None)
    seen=[]
    def invoke(a,b,phase,v,caller,limit,correction,callback,*,prepared):
        seen.append(prepared)
        monkeypatch.setattr(semantic,'prepare_request_projection',lambda *_:(_ for _ in ()).throw(AssertionError('second projection')))
        return callback()
    monkeypatch.setattr(calls,'invoke',invoke)
    def dispatch(*args,prepared,**kwargs):
        assert prepared is seen[0]
        return 'fixed request',prepared['wire']
    monkeypatch.setattr(semantic,'_dispatch_model_call',dispatch)
    result=semantic.model_call(adapter,binding,'citation',view,None,30000)
    assert result[0]=='fixed request' and len(seen)==1
