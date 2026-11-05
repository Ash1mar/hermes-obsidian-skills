"""Future-only dispatch/recovery; all responses are isolated synthetic slots."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from test_page_call_contracts import system,item,reserve,returned,settle
from page_pair_calls import key,PageCalls

def test_saved_response_crosses_epoch_only_with_current_authorization(system):
    b,state,writes,calls,validate,persist=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'isolated a'},'p2':{'content':'isolated c'}})
    def interrupt(_):raise RuntimeError('stopped after first receipt')
    with pytest.raises(RuntimeError):settle(system,q,after_page=interrupt)
    receipt=copy.deepcopy(b.status(*a)['receipt']);state['epoch']='authorized-resume'
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):settle(system,q)
    b.settle(q,validate,persist,lambda *_:True,recovery_epoch=state['epoch'])
    assert b.status(*a)['receipt']==receipt and calls.count(key(a[0]))==1 and len(writes)==2
    assert all(len(row['attempts'])==1 for row in b.read()['pages'].values())
    state['cancelled']=True
    with pytest.raises(ContractError):b.settle(q,validate,persist,lambda *_:True,recovery_epoch=state['epoch'])

def test_image_reservation_identity_is_bound_and_tampering_is_held(system):
    b,_,_,_,validate,persist=system;a=item('asset')
    images=[{'url':'data:image/png;base64,AA==','sha256':'pinned','bytes':1,'references':[]}]
    q=b.reserve([a],{'system':'fixed','user':'image label'}, {},images)
    returned(b,q,{'p1':{'content':'isolated asset'}})
    r=b.read();assert 'url' not in r['requests'][q]['image_inputs'][0]
    r['requests'][q]['image_inputs'][0]['sha256']='changed';b.save(r)
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):settle(system,q)

def test_pair_encoding_is_exact_for_null_missing_qa_assets_and_context():
    from fixed_page_evidence import present_pair,restore_pair
    v={'pages':{'p1':{'candidates':[{'candidate':'c1','evidence_qa':[{'qa':'limits'}],'exceptions':None}]},
                'p2':{'candidates':[{'candidate':'c2','evidence_qa':[{'qa':'limits'}],'source_evidence':[]}]}},
        'original_evidence':{'tasks':[{'materials':[{'ref':'m1','asset':{'required':True}},{'ref':'m2','text':'contrary context'}]}]}}
    original=copy.deepcopy(v)
    assert restore_pair(present_pair(v))==original and v==original

def test_new_pin_precedes_consumption_and_old_pin_is_never_upgraded(tmp_path):
    from efficiency_presentation import pin
    w={'workflow_id':'isolated','scope':{'vault':'isolated'}};workflow=NS(vault=tmp_path)
    p=pin(workflow,w);assert p['fixed_before_consumption'] and p['page_dispatch_contract']=='per-page-dispatch/v2'
    root=tmp_path/'_system/ledgers/ingest-workflows/isolated'
    old={k:v for k,v in p.items() if k not in ('fixed_before_consumption','page_dispatch_contract')}
    (root/'presentation-contract.json').write_text(json.dumps(old))
    assert pin(workflow,w)==old

def test_one_oversize_page_does_not_wholesale_fallback(tmp_path,monkeypatch):
    import native_page_pairs as native,semantic_output,reduce_disposition,fixed_global_packets,qa_program
    a=NS(workflow=NS(vault=tmp_path,status=lambda _: {'state':'running'}),worker_check=lambda _:None)
    import hermes_source_units.delivery_scope as delivery
    a.workflow.knowledge=NS();monkeypatch.setattr(delivery,'guard_page',lambda *_:None)
    b={'workflow_id':'isolated','node':'global-reduce','actor':'agent'}
    pages=[{'identity':{'kind':'entity','identity_key':'big'},'path':'big.md','candidate_refs':['c1']}]
    value={'view':{'page_call_contract':'durable-page-calls/v1','page_dispatch_contract':'per-page-dispatch/v2'}}
    monkeypatch.setattr(semantic_output,'output_boundary',lambda *_:(None,None))
    monkeypatch.setattr(reduce_disposition,'page_permit',lambda *_:None)
    holds=[]
    def isolate(a,b,v,s,w,h,p):holds.extend(h);return ({**s,'pages':[]},'isolated-gap')
    monkeypatch.setattr(qa_program,'isolate_pages',isolate)
    monkeypatch.setattr(qa_program,'page_link_failures',lambda *_:[])
    monkeypatch.setattr(fixed_global_packets,'complete',lambda v,s:{'pages':s['pages']})
    result,_=native.execute(a,b,value,{'pages':pages},None,lambda *_:pytest.fail('no call'),lambda *_:None,None,
        lambda *args:({'original_evidence':{'tasks':[]}},30001))
    assert result=={'pages':[]} and holds[0]['code']=='READING_WINDOW_OVERSIZE'
    assert not (tmp_path/'_system/ledgers/ingest-workflows/isolated/page-call-budget.json').exists()

def test_future_entry_uses_shared_ledger_after_page_measurement(tmp_path,monkeypatch):
    import fixed_page_work as work,native_page_pairs as native,reduce_coverage,reduce_disposition,semantic_output
    a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'isolated','node':'global-reduce'}
    page={'identity':{'kind':'entity','identity_key':'a'},'path':'a.md','candidate_refs':['c1']}
    value={'input_id':'isolated','view':{'page_call_contract':'durable-page-calls/v1','page_dispatch_contract':'per-page-dispatch/v2'}}
    monkeypatch.setattr(reduce_coverage,'current',lambda *_:None)
    monkeypatch.setattr(reduce_disposition,'page_permit',lambda *_:None)
    monkeypatch.setattr(semantic_output,'output_boundary',lambda *_:(None,None))
    monkeypatch.setattr(work,'measure',lambda *_:({'candidates':[]},100))
    visits=[]
    def shared(*args,**kw):visits.append((args[3],kw));return {'pages':[]},[]
    monkeypatch.setattr(native,'execute',shared)
    result=work.execute(a,b,value,{'pages':[page]},None,lambda *_:pytest.fail('legacy body call'),None,None)
    assert result==({'pages':[]},[]) and visits[0][0]['pages']==[page]
