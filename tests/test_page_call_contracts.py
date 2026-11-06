"""Mechanical page contracts. Only isolated paths and synthetic response fixtures."""
import copy,json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units import ContractError
from page_pair_calls import PageCalls,key
from page_dependencies import derive,provenance

def item(name):
    return ({'identity':{'kind':'entity','identity_key':name},'path':name+'.md'},
            {'path':name+'.md','candidates':[name],'evidence_sha256':'fixed'})

@pytest.fixture
def system(tmp_path):
    state={'epoch':'initial','cancelled':False};writes={};persist_calls=[]
    def guard():
        if state['cancelled']:raise ContractError('WORKFLOW_STOPPED','$','cancelled')
        return state['epoch']
    book=PageCalls(tmp_path,guard)
    def validate(k,draft):
        if not isinstance(draft,dict) or set(draft)!={'content'} or not isinstance(draft['content'],str):
            code='SEMANTIC_REVIEW_REQUIRED' if isinstance(draft,dict) and 'blocked_reason' in draft else 'INVALID_SCHEMA'
            raise ContractError(code,'$','synthetic page output validation')
        return draft
    def persist(k,draft,request,audit):
        persist_calls.append(k)
        if k in writes:assert writes[k]==draft
        writes[k]=copy.deepcopy(draft)
        return {'draft_ref':k+'.md'}
    return book,state,writes,persist_calls,validate,persist

def reserve(book,*items):return book.reserve(list(items),{'system':'fixed','user':'isolated wire'}, {'type':'object'})
def returned(book,q,slots):book.capture(q,json.dumps({'results':slots}),'isolated-audit',{'origin':'isolated_fixture'})
def settle(system,q,**kw):
    book,_,_,_,validate,persist=system
    return book.settle(q,validate,persist,lambda q,r:True,**kw)

def test_atomic_reservation_all_or_none(system):
    b,*_=system;a,c=item('a'),item('c');q=reserve(b,a)
    before=b.path.read_bytes()
    with pytest.raises(ContractError):reserve(b,c,a)
    assert b.path.read_bytes()==before and key(c[0]) not in b.read()['pages']

def test_one_page_failure_retains_success_and_independent_receipt(system):
    b,_,writes,calls,*_=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'fixture a'},'p2':{'invalid':'fixture'}})
    result=settle(system,q)
    assert result[key(a[0])]['state']=='valid' and result[key(c[0])]['state']=='rejected'
    assert len(writes)==1
    q2=reserve(b,c);returned(b,q2,{'p1':{'content':'fixture c'}});settle(system,q2)
    settle(system,q);settle(system,q2)
    assert calls.count(key(a[0]))==1 and len(b.read()['pages'][key(c[0])]['attempts'])==2

def test_crash_after_first_receipt_does_not_redo_it(system):
    b,_,writes,calls,*_=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'fixture a'},'p2':{'content':'fixture c'}})
    def crash(k):raise RuntimeError('isolated interruption')
    with pytest.raises(RuntimeError):settle(system,q,after_page=crash)
    assert len(writes)==1 and b.status(*a)['receipt']['state']=='valid'
    system=(PageCalls(b.root,b.guard),*system[1:]);settle(system,q)
    assert calls.count(key(a[0]))==1 and len(writes)==2

def test_crash_after_body_before_receipt_reuses_raw_response(system):
    b,_,writes,calls,validate,persist=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'fixture a'},'p2':{'content':'fixture c'}})
    def interrupt(*args):persist(*args);raise RuntimeError('after durable body')
    with pytest.raises(RuntimeError):b.settle(q,validate,interrupt,lambda *_:True)
    assert len(writes)==1 and 'receipt' not in b.status(*a)
    settle(system,q);assert len(writes)==2 and 'response' in b.read()['requests'][q]

def test_duplicate_receipt_is_idempotent(system):
    b,_,_,calls,*_=system;a=item('a');q=reserve(b,a);returned(b,q,{'p1':{'content':'fixture'}})
    one=settle(system,q);two=settle(system,q)
    assert one==two and len(calls)==1

def test_cancel_between_pages_prevents_second_settlement(system):
    b,state,writes,_,*_=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'fixture a'},'p2':{'content':'fixture c'}})
    def cancel(k):state['cancelled']=True;state['epoch']='stopped'
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):settle(system,q,after_page=cancel)
    assert len(writes)==1 and 'receipt' in b.status(*a) and 'receipt' not in b.status(*c)
    state['cancelled']=False
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):settle(system,q)

def test_unknown_does_not_resend_or_release_budget(system):
    b,*_=system;a,c=item('a'),item('c');q=reserve(b,a,c);before=b.path.read_bytes()
    with pytest.raises(ContractError,match='unknown'):settle(system,q)
    with pytest.raises(ContractError,match='unknown'):reserve(b,a,item('d'))
    assert b.path.read_bytes()==before

def test_regrouping_cannot_reset_page_additional_budget(system):
    b,*_=system;a,c,d=item('a'),item('c'),item('d');q=reserve(b,a,c)
    returned(b,q,{'p1':{},'p2':{'content':'fixture c'}});settle(system,q)
    q2=reserve(b,a,d);returned(b,q2,{'p1':{},'p2':{'content':'fixture d'}});settle(system,q2)
    before=b.path.read_bytes()
    with pytest.raises(ContractError,match='exhausted'):reserve(b,a,item('new-peer'))
    assert b.path.read_bytes()==before

def test_evidence_refusal_does_not_consume_automatic_additional_check(system):
    b,*_=system;a=item('a');q=reserve(b,a);returned(b,q,{'p1':{'blocked_reason':'isolated missing evidence'}});settle(system,q)
    with pytest.raises(ContractError,match='not correctable'):reserve(b,a)
    assert len(b.status(*a)['attempts'])==1

def test_changed_input_and_path_cannot_reset_budget(system):
    b,*_=system;a=item('a');q=reserve(b,a);returned(b,q,{'p1':{}});settle(system,q)
    for changed in ((a[0],{**a[1],'new':'different evidence'}),({**a[0],'path':'new.md'},a[1])):
        with pytest.raises(ContractError,match='STALE_INPUT'):reserve(b,changed)

@pytest.mark.parametrize('damage',['raw','members','receipt','native_proof'])
def test_damaged_raw_mapping_or_receipt_holds(system,damage):
    b,_,_,_,validate,persist=system;a=item('a');q=reserve(b,a);returned(b,q,{'p1':{'content':'fixture'}})
    if damage=='receipt':settle(system,q)
    r=b.read()
    if damage=='raw':r['requests'][q]['response']['raw']='tampered'
    if damage=='members':r['requests'][q]['members']['p1']['page_key']='other'
    if damage=='receipt':r['pages'][key(a[0])]['receipt']['result']['content']='tampered'
    b.save(r)
    with pytest.raises(ContractError):b.settle(q,validate,persist,lambda *_:damage!='native_proof')

def packet():
    m={'ref':'m1','role':'core','heading':0,'text_ref':'x','source_span':{'start':0,'end':20}}
    p={'tasks':[{'task':'t1','materials':[m,{**m,'ref':'m2'},{'ref':'m3','role':'context','heading':0,'text_ref':'contrary'}], 'limits':{'omitted':1}}],
       'texts':{'x':'scope AND condition','contrary':'except when NOT eligible'},'headings':{'0':['Scope','Conditions']},'qa':{'q1':'conversion limitation'}}
    refs={'m1':{'unit_ref':{'artifact_revision':'v1','unit_id':'u1'},'span':None},
          'm2':{'unit_ref':{'artifact_revision':'v1','unit_id':'u1'},'span':None},
          'm3':{'unit_ref':{'artifact_revision':'v1','unit_id':'u3'},'span':None}}
    proof=provenance(p,{('task-1','package-hash'):{'reading':'t1','mapping':refs}})
    c=[{'candidate':'c1','applicability':'scope','conditions':['AND'],'exceptions':['NOT eligible'],
        'source_evidence':[{'reading':'t1','support_refs':['m1']}],'evidence_qa':[{'qa':'usable','qa_note':''}]}]
    return p,c,proof

def test_dependency_proof_preserves_contrary_context_conditions_qa_and_versions():
    p,c,proof=packet();v,m=derive(p,c,proof)
    assert [r['ref'] for r in v['tasks'][0]['materials']]==['m1','m3']
    assert v['texts']==p['texts'] and v['qa']==p['qa'] and v['headings']==p['headings']
    assert m['candidate_scope']==c and m['original_sha256']==proof['packet_sha256']
    ds=m['readings'][0]['decisions'];assert next(d for d in ds if d['ref']=='m3')['decision']=='retain'
    assert next(d for d in ds if d['ref']=='m2')['equivalent_retained_ref']=='m1'

@pytest.mark.parametrize('difference',['qa_ref','heading','required_dependency','source_version','text'])
def test_duplicate_exclusion_requires_full_equivalence(difference):
    p,c,proof=packet();row=p['tasks'][0]['materials'][1]
    if difference=='source_version':proof['readings']['t1']['mapping']['m2']['unit_ref']['artifact_revision']='v2'
    elif difference=='text':row['text_ref']='contrary'
    elif difference=='required_dependency':row[difference]=True
    elif difference=='heading':row[difference]=1;p['headings']['1']=['Exceptions']
    else:row[difference]='q1'
    proof['packet_sha256']=__import__('hermes_source_units.validation',fromlist=['fingerprint']).fingerprint(p)
    v,m=derive(p,c,proof);assert len(v['tasks'][0]['materials'])==3

def test_unproven_context_and_stale_proof_fall_back_without_model():
    p,c,proof=packet();proof['packet_sha256']='stale';v,m=derive(p,c,proof)
    assert v==p and all(d['decision']=='retain' for d in m['readings'][0]['decisions'])

def test_exact_reference_not_found_is_held():
    p,c,proof=packet();c[0]['source_evidence'][0]['support_refs']=['m99']
    with pytest.raises(ContractError,match='UNRESOLVED_REFERENCE'):derive(p,c,proof)

def test_page_ack_is_published_before_second_page_failure(system):
    b,*_=system;a,c=item('a'),item('c');q=reserve(b,a,c)
    returned(b,q,{'p1':{'content':'fixture a'},'p2':{}});acks=[]
    def observe(k):
        assert acks and acks[0]['page_key']==key(a[0])
        raise RuntimeError('interrupt before peer validation')
    with pytest.raises(RuntimeError):settle(system,q,on_receipt=acks.append,after_page=observe)
    assert b.status(*a)['receipt']==acks[0] and 'receipt' not in b.status(*c)


@pytest.mark.parametrize('cancel_after_first',[False,True])
@pytest.mark.parametrize('dispatch',[None,'per-page-dispatch/v2','unified-page-execution/v3'])
@pytest.mark.parametrize('special',[None,'oversize','asset'])
def test_native_pair_bridge_recovers_receipts_without_new_call(tmp_path,monkeypatch,cancel_after_first,dispatch,special):
    if special and dispatch=='unified-page-execution/v3':pytest.skip('new protocol uses complete-request budget/asset fixtures, not the historical fake size probe')
    if special and (not dispatch or cancel_after_first):pytest.skip('mixed-page cases use future dispatch without interruption')
    from types import SimpleNamespace as NS
    import native_page_pairs as native
    import semantic_output,reduce_disposition,fixed_semantic_recovery,fixed_global_packets
    from page_pair_calls import capture
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    from hermes_source_units.validation import fingerprint
    p,c,proof=packet();p['qa']={};p['tasks'][0].pop('limits')
    proof['packet_sha256']=fingerprint(p)
    pages=[{'identity':{'kind':'entity','identity_key':name},'path':name+'.md','candidate_refs':[name]} for name in ('a','b')]
    views={}
    for page in pages:
        candidate=copy.deepcopy(c);candidate[0]['candidate']=page['candidate_refs'][0]
        evidence,manifest=derive(p,candidate,proof)
        views[page['path']]={'identity':page['identity'],'path':page['path'],'candidates':candidate,
                              'original_evidence':evidence,'evidence_selection':manifest,'existing_page':None}
    w={'cancel_requested':False,'state':'running'}
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:w),worker_check=lambda _:None)
    binding={'workflow_id':'isolated','node':'global-reduce','actor':'agent'}
    state={'pages':pages};value={'view':{'page_call_contract':'durable-page-calls/v1',
        'candidates':[{'candidate':p['candidate_refs'][0],'source_evidence':[]} for p in pages],
        'original_evidence':{'tasks':[]}},'backend':{'existing_pages':{}}}
    if dispatch:value['view']['page_dispatch_contract']='per-page-dispatch/v2'
    if dispatch=='unified-page-execution/v3':
        value['view']['page_execution_contract']=dispatch
        for view in views.values():view.update(page_execution_contract=dispatch,page_scope_contract='assigned-candidates/v1')
    adapter.workflow.knowledge=NS()
    import hermes_source_units.delivery_scope as delivery
    monkeypatch.setattr(delivery,'guard_page',lambda *_:None)
    if special:
        extra={'identity':{'kind':'entity','identity_key':'extra'},'path':'extra.md','candidate_refs':['extra']}
        pages.insert(0,extra);views['extra.md']=copy.deepcopy(views['a.md']);views['extra.md']['path']='extra.md'
        if special=='asset':
            import fixed_semantic_media as media
            views['extra.md']['original_evidence']['tasks'][0]['materials'][0]['asset']={'path':'isolated.png'}
            image={'url':'data:image/png;base64,AA==','sha256':'isolated','bytes':1,'references':[{'task':'t1','material':'m1'}]}
            monkeypatch.setattr(media,'image_catalog',lambda *_:{})
            monkeypatch.setattr(media,'select_images',lambda *_:[image])
        import qa_program
        monkeypatch.setattr(qa_program,'page_link_failures',lambda *_:[])
        monkeypatch.setattr(qa_program,'isolate_pages',lambda a,b,v,s,w,h,p:({**s,'pages':[x for x in s['pages'] if x['path'] not in {r['path'] for r in h}]},'fixture-gap'))
    monkeypatch.setattr(semantic_output,'output_boundary',lambda *_:(None,None))
    monkeypatch.setattr(reduce_disposition,'page_permit',lambda *_:None)
    original=PageCalls
    monkeypatch.setattr(native,'PageCalls',lambda root,guard,transaction,**kw:original(root,guard,**kw))
    monkeypatch.setattr(fixed_global_packets,'complete',lambda value,s:{'pages':copy.deepcopy(s['pages'])})
    raw_by={};calls=[];receipts=[]
    def model(adapter,binding,phase,view,caller,limit,**kw):
        calls.append(view);raw=json.dumps({'results':{k:{'content':'isolated mechanical slot '+k} for k in view['pages']}})
        ref=f"_system/ledgers/ingest-workflows/isolated/semantic-calls/{len(calls)}.json"
        _write_atomic(tmp_path/ref,_json_bytes({'fixture':True,'phase':'page-pair','page_request_id':__import__('page_pair_calls').ACTIVE.get()[1],'model':{'origin':'isolated_fixture'}}));raw_by[str(tmp_path/ref)]=raw
        capture(adapter,binding,raw,ref,{'origin':'isolated_fixture'});return raw,ref
    monkeypatch.setattr(fixed_semantic_recovery,'native_reply',lambda a,b,path,*_,**kw:(raw_by[str(path)],{}))
    cancelled=[]
    def ack(a,r,x):
        receipts.append(x)
        if cancel_after_first and not cancelled:
            cancelled.append(True);w['cancel_requested']=True
    from fixed_semantic import prepare_request_projection
    args=(adapter,binding,value,state,object(),model,ack,prepare_request_projection if dispatch=='unified-page-execution/v3' else lambda *a:None,
          lambda a,v,p,c,pr:(copy.deepcopy(views[p['path']]),30001 if special=='oversize' and p['path']=='extra.md' else 100))
    if cancel_after_first:
        with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):native.execute(*args)
        root=tmp_path/'_system/ledgers/ingest-workflows/isolated'
        assert sum('receipt' in r for r in PageCalls(root,lambda:None).read()['pages'].values())==1
        w['cancel_requested']=False
    result,refs=native.execute(*args)
    expected_calls=2 if special=='asset' else 1;expected_pages=3 if special=='asset' else 2
    assert len(calls)==expected_calls and any(len(c['pages'])==2 for c in calls) and len(result['pages'])==expected_pages
    assert len({r['page_receipt_id'] for r in receipts})==expected_pages
    native.execute(*args);assert len(calls)==expected_calls

def test_concurrent_reservations_cannot_double_spend(system):
    from concurrent.futures import ThreadPoolExecutor
    b,*_=system;a,c=item('a'),item('c')
    def attempt(_):
        try:return reserve(PageCalls(b.root,b.guard),a,c)
        except ContractError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1
    assert all(len(r['attempts'])==1 for r in b.read()['pages'].values())


def test_cancel_before_atomic_publication_reserves_nothing(tmp_path):
    calls=[]
    def guard():calls.append(1);return str(len(calls))
    b=PageCalls(tmp_path,guard)
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):reserve(b,item('a'),item('b'))
    assert not b.path.exists()

def test_recovery_fingerprint_includes_page_contract_modules(tmp_path,monkeypatch):
    import qa_program
    names=('page_protocol.py','page_pair_eligibility.py','page_budget_store.py','split_page_calls.py','page_request.py','qa_program.py','fixed_page_work.py','fixed_semantic.py','native_page_pairs.py','page_pair_calls.py','page_dependencies.py','durable_page_replay.py','page_budget_hold.py',
        'source_unknown_recovery.py','native_stream_policy.py','returned_native_output.py','network_connection_retry.py')
    for name in names:(tmp_path/name).write_text('isolated code '+name)
    monkeypatch.setattr(qa_program,'__file__',str(tmp_path/'qa_program.py'))
    before=qa_program.execution_code_fingerprint()
    for name in names:
        p=tmp_path/name;old=p.read_text();p.write_text('changed isolated code')
        assert qa_program.execution_code_fingerprint()!=before;p.write_text(old)

def test_page_groups_preserve_configured_concurrency_bound():
    from native_page_pairs import run_groups
    import threading
    lock=threading.Lock();barrier=threading.Barrier(2);counts={'active':0,'peak':0};done=[]
    def submit(group):
        with lock:counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
        barrier.wait(timeout=5)
        with lock:counts['active']-=1;done.append(group)
    run_groups([1,2,3,4],submit,2)
    assert counts=={'active':0,'peak':2} and sorted(done)==[1,2,3,4]
