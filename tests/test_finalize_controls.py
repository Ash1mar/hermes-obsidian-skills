"""No task/operation/wording reset of a page's consumed local call allowance."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from test_partial_finalize import writer
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.partial_finalize import pin
from hermes_source_units.finalize_revision_state import append,load,work_id
import finalize_revision as revision
import finalize_disposition_control as control

def setup(tmp_path):
    wid='fixture';run='run';pid='page';key=work_id(run,pid)
    view={'page':'e1','path':'30_Cards/page.md','identity':{'name':'page'},'content':'original',
        'existing_page':None,'original_evidence':{'tasks':[]},'citation_evidence':[]}
    work={'work_id':key,'run_id':run,'page_id':pid,'original_revision_id':'original',
        'baseline_calls':{'page-write':2,'page-review':1},'issue_ids':['issue'],'state':'revision_authorized',
        'grants':[{'grant_id':'grant','material_sha256':control.material_identity(view),
            'basis':{'kind':'verified_review_error','reason':'A verified contradictory review'},
            'mode':'rewrite_and_review','limits':{'page-write':1,'page-review':1},'calls':{}}]}
    append(tmp_path,wid,work,'authorize','revise',{})
    service=NS(vault=tmp_path);adapter=NS(workflow=NS(vault=tmp_path))
    binding={'workflow_id':wid,'task_id':'native','node':'build-finalize','actor':'agent'}
    return service,adapter,binding,work,view

def test_unknown_call_is_durable_and_cannot_retry_under_new_task_or_text(tmp_path,monkeypatch):
    service,a,b,work,view=setup(tmp_path);calls=[]
    def interrupted(adapter,binding,phase,view,*args,**kwargs):
        calls.append(phase)
        from semantic_output import path_for
        p=path_for(adapter,binding,phase,view);p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'attempts':[{'state':'reserved'}]}))
        raise ContractError('NATIVE_COMPLETION_UNKNOWN','$','isolated unknown')
    monkeypatch.setattr('semantic_output.execute',interrupted)
    with writer(service),pytest.raises(ContractError):revision.execute_call(a,b,work,'page-write',view,None,None)
    saved=load(tmp_path,'fixture')['works'][work['work_id']]
    assert saved['grants'][0]['calls']['page-write']['cumulative_ordinal']==3
    assert saved['grants'][0]['calls']['page-write']['state']=='held'
    for changed in (view,{**view,'cosmetic_instruction':'new words'}):
        with writer(service),pytest.raises(ContractError):
            revision.execute_call(a,{**b,'task_id':'replacement'},saved,'page-write',changed,None,None)
    assert calls==['page-write']

def test_unreserved_controlled_request_cannot_dispatch_even_if_new_id(tmp_path):
    _,a,b,work,view=setup(tmp_path)
    from fixed_semantic import _model_call
    with pytest.raises(ContractError,match='no fresh cumulative ordinal'):
        _model_call(a,b,'page-write',{**view,'finalize_revision':{'instruction':'new ID'}},None,30000)

def test_control_history_hash_tamper_does_not_grant_a_new_allowance(tmp_path):
    _,_,_,work,_=setup(tmp_path)
    state=load(tmp_path,'fixture');path=tmp_path/state['head']['ref']
    value=json.loads(path.read_text());value['work']['grants'][0]['limits']['page-write']=100
    path.write_bytes(_json_bytes(value))
    with pytest.raises(ContractError):load(tmp_path,'fixture')

def controller(tmp_path,monkeypatch,status='revision_required'):
    wid='fixture';run={'run_id':'run','revision':1,'page_revisions':[{'page_id':'page'}]}
    item={'issue_id':'issue','logical_work':{'run_id':'run','page_id':'page','revision_id':'original'},
        'page':'e1','state':'deferred','observed_status':status,'history':{'attempt_count':1}}
    view={'page':'e1','path':'30_Cards/page.md','identity':{},'existing_page':None,'original_evidence':{},'citation_evidence':[]}
    snapshot={'backend':{'runs':[run]},'views':[view]};inv={'items':[item],'snapshot':{'ref':'snapshot','sha256':'b'}}
    (tmp_path/'run.json').write_text(json.dumps(run))
    service=NS(vault=tmp_path,_check_request=lambda _:None,_lock=lambda _:tmp_path/'workflow.lock',
        knowledge=NS(_run_path=lambda _:tmp_path/'run.json'))
    adapter=NS(workflow=service)
    monkeypatch.setattr(control,'inspect',lambda *_:({'actor':'agent'},inv,snapshot,{'ok':True}))
    monkeypatch.setattr(control,'original_calls',lambda *_:{'page-write':2,'page-review':1})
    monkeypatch.setattr(control,'progress',lambda *_:{'committed':{}})
    basis_path=tmp_path/'new-evidence.json';basis_path.write_text('{"finding":"verified review contradiction"}')
    basis={'kind':'verified_review_error','reason':'Verified against originals','evidence':[pin(tmp_path,'new-evidence.json')]}
    request={'workflow_id':wid,'actor':'agent','expected_revision':1,'expected_control_revision':0,
        'operation_id':'operation','action':'defer','run_id':'run','issue_id':'issue','inventory':{'ref':'inv','sha256':'a'},
        'category':'unresolved','reason':'Preserve pending attribution','input_digest':'request'}
    return adapter,request,basis,view

def test_unknown_execution_cannot_be_content_deferred(tmp_path,monkeypatch):
    a,req,_,_=controller(tmp_path,monkeypatch,'completion_unknown')
    with pytest.raises(ContractError,match='cannot be deferred as a content'):
        control.apply(a,req)
    assert load(tmp_path,'fixture')['revision']==0

def test_defer_reopen_and_unchanged_material_budget_cannot_reset(tmp_path,monkeypatch):
    a,req,basis,view=controller(tmp_path,monkeypatch)
    control.apply(a,req)
    key=work_id('run','page');first=load(tmp_path,'fixture')['works'][key]
    assert first['state']=='deferred' and first['baseline_calls']['page-write']==2
    reopened={**req,'action':'reopen','basis':basis,'operation_id':'reopen','expected_control_revision':1,'input_digest':'reopen-request'}
    control.apply(a,reopened)
    state=load(tmp_path,'fixture');work=copy.deepcopy(state['works'][key])
    work['grants']=[{'grant_id':'already-used','material_sha256':control.material_identity(view),
        'limits':{'page-write':1,'page-review':1},'calls':{'page-write':{'state':'valid'}}}]
    append(tmp_path,'fixture',work,'existing-consumption','reserve',{})
    revised={**reopened,'action':'revise','operation_id':'new-operation','input_digest':'different-wording',
        'expected_control_revision':3}
    with pytest.raises(ContractError,match='already consumed'):
        control.apply(a,revised)
    assert len(load(tmp_path,'fixture')['works'][key]['grants'])==1

@pytest.mark.parametrize('mode',['review_only','rewrite_and_review'])
def test_review_only_of_existing_native_reduce_draft_does_not_require_a_separate_write_call(tmp_path,monkeypatch,mode):
    a,req,basis,view=controller(tmp_path,monkeypatch)
    monkeypatch.setattr(control,'original_calls',lambda *_:{'page-write':0,'page-review':0})
    reopen={**req,'action':'reopen','basis':basis,'operation_id':'open-reduce-draft','input_digest':'open'}
    control.apply(a,reopen)
    revise={**reopen,'action':'revise','mode':mode,'expected_control_revision':1,'operation_id':'bounded-review','input_digest':'bounded'}
    if mode=='rewrite_and_review':
        with pytest.raises(ContractError,match='page-generation call history'):control.apply(a,revise)
    else:
        result=control.apply(a,revise)
        assert result['new_calls_granted']==1 and result['work']['grants'][0]['limits']=={'page-write':0,'page-review':1}
