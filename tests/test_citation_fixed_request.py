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
    binding={**binding,'node':'resource-reduce:r'}
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


def test_explicit_one_call_extension_is_atomic_idempotent_and_keeps_unknown_charged(tmp_path,monkeypatch):
    import hashlib
    from hermes_source_units.source_units import _write_atomic,_json_bytes,_load_json
    import citation_amendment_control as control
    wid,binding,adapter,view,p=setup(tmp_path,monkeypatch)
    value=core.state(tmp_path,wid);frozen=core.plan(tmp_path,value)
    frozen['cumulative_limits']['citation']=1;frozen['basis']=[{'ref':'old','sha256':'a'*64}]
    path=tmp_path/value['plan_ref'];_write_atomic(path,_json_bytes(frozen))
    value['plan_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();core.save(tmp_path,wid,value)
    reservation=core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],binding)
    ledger=core.pending_calls(tmp_path,wid);ledger['limits']={'original-task':frozen['cumulative_limits']}
    del ledger['jobs']['citation:original-task']['attempts'][0]['events']
    _write_atomic(reservation[0],_json_bytes(ledger))
    old=ledger['jobs']['citation:original-task']['attempts'][0]
    w={'workflow_id':wid,'actor':'agent','revision':1,'kanban':{'board_id':'isolated'}}
    adapter.workflow.status=lambda _:w;adapter.workflow._mutation=lambda _:w
    monkeypatch.setattr(control,'drained',lambda *_:None)
    evidence=tmp_path/'fixed.json';_write_atomic(evidence,_json_bytes({'cause':'distinct corrected program cause'}))
    req={'workflow_id':wid,'actor':'agent','expected_revision':1,'recovery_id':value['operation_id'],
        'action':'authorize-call','target_contract_sha256':control.contract_hash(),'logical_work':'citation:original-task',
        'supersedes_attempt_sha256':fingerprint(old),'reason':'Explicit isolated one-call authorization.',
        'basis':[{'ref':'fixed.json','sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}],
        'cumulative_limit':2}
    result=control.recover(adapter,req);assert result['additional_calls']==1
    before=reservation[0].read_bytes()
    result=control.recover(adapter,req);assert result['replayed'] and result['additional_calls']==0
    assert reservation[0].read_bytes()==before
    status=calls.execution_admission(adapter,wid,binding['node'])
    assert status[0]['state']=='controlled_regeneration_authorized'
    assert status[0]['prior_execution_state']=='completion_unknown'
    core.reserve(tmp_path,wid,'citation:original-task',p['wire'],p['identity']['output_schema'],{**binding,'task_id':'new'})
    final=core.pending_calls(tmp_path,wid)
    assert final['limits']['original-task']['citation']==1
    assert core.lineage_limits(final,'original-task',frozen['cumulative_limits'])['citation']==2
    assert final['jobs']['citation:original-task']['attempts'][0]==old
    assert len(final['jobs']['citation:original-task']['attempts'])==2
    assert not (core.root(tmp_path,wid)/'pending.json').exists()
