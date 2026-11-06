"""Faults use isolated fake responses; they do not establish native acceptance."""
import copy,hashlib,json
from contextlib import nullcontext
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.input_budget import profile
from page_budget_store import Snapshot,operation
from page_pair_calls import ACTIVE
from split_page_calls import SplitPageCalls
from test_global_plan_inputs import fixture_value
import paired_reviews as pair

@pytest.fixture
def review_system(tmp_path,monkeypatch):
    import hermes_source_units.workflow_guard as guard
    import fixed_semantic_recovery,finalize_revision
    monkeypatch.setattr(guard,'worker_binding',lambda *_:nullcontext())
    monkeypatch.setattr(guard,'workflow_write_guard',lambda *_a,**_k:nullcontext())
    monkeypatch.setattr(finalize_revision,'work_for',lambda *_:None)
    source=fixture_value();views=[];pages={}
    for i in (1,2):
        alias='e'+str(i);pid='page-'+str(i);body='Native fixture body '+str(i)
        view={'page':alias,'identity':{'kind':'entity','identity_key':pid},'path':pid+'.md','content':body,
            'citation_evidence':[copy.deepcopy(source['view']['candidates'][i-1])],
            'original_evidence':copy.deepcopy(source['view']['original_evidence'])}
        view['original_evidence']['texts']['s1']='Private source '+str(i)+' AND conditions. Do not transfer to another page.'
        views.append(view);pages[alias]={'page':{'page_id':pid,'revision_id':'rev-'+str(i),'parent_revision_id':None,
            'authored_sha256':hashlib.sha256(body.encode()).hexdigest()},'assessment':{'policy':'explicit-page-risks/v1',
            'risk_reasons':['different_applicability'],'evidence_sha256':str(i)*64}}
    value={'views':views,'backend':{'pages':pages,'input_budget':profile('review-40000')}}
    binding={'workflow_id':'isolated','node':'build-finalize','task_id':'native','actor':'operator','template_hash':'fixed'}
    adapter=NS(workflow=NS(vault=tmp_path),worker_check=lambda *_:{})
    epoch={'value':None};monkeypatch.setattr(pair,'epoch',lambda *_:epoch['value'])
    raw_by_audit={};calls=[];attached=[]
    def proof(_a,_b,path,wire,sc,images):
        row=raw_by_audit.get(path.relative_to(tmp_path).as_posix())
        assert row and row[1]==wire
        return row[0],json.loads(path.read_text())
    monkeypatch.setattr(fixed_semantic_recovery,'native_reply',proof)
    def call(_a,_b,phase,view,caller,limit,**kwargs):
        assert phase=='page-review-pair' and limit==40000
        book,rid=ACTIVE.get();q=book.read()['requests'][rid];calls.append(copy.deepcopy(view))
        slots={s:{'decision':'approved','review_note':'Independent fixture conclusion '+v['review_page_id']} for s,v in view['pages'].items()}
        if len(calls)==1 and mode['value']=='partial':slots['p2']={'invalid':'mechanical fixture failure'}
        if len(calls)==1 and mode['value']=='negative':slots['p2']={'decision':'revision_required','review_note':'Concrete fixture defect'}
        raw=json.dumps({'results':slots});ref=f'_system/ledgers/ingest-workflows/isolated/semantic-calls/{len(calls)}.json'
        metadata={'origin':'hermes_native_model','fixture_only':True,'response_sha256':fingerprint(raw)}
        audit={'contract':'hermes-fixed-semantic-call/v1','workflow_id':'isolated','node':'build-finalize',
            'phase':phase,'page_request_id':rid,'model':metadata,'input_sha256':fingerprint(q['wire']),
            'schema_sha256':fingerprint(q['schema']),'response_sha256':fingerprint(raw)}
        path=tmp_path/ref;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(audit))
        raw_by_audit[ref]=(raw,q['wire']);book.capture(rid,raw,ref,metadata)
        if mode['value']=='capture-crash':raise RuntimeError('after durable response before page receipts')
        return raw,ref
    mode={'value':'valid'}
    def run():return pair.execute(adapter,binding,value,object(),call,lambda *a:attached.append(a),{})
    return NS(adapter=adapter,binding=binding,value=value,calls=calls,mode=mode,run=run,epoch=epoch,root=pair.folder(adapter,binding),proofs=raw_by_audit)

def test_two_independent_evidence_sets_form_two_durable_conclusions(review_system):
    s=review_system;out=s.run();assert len(s.calls)==1 and len(out)==2
    assert s.calls[0]['pages']['p1']['original_evidence']!=s.calls[0]['pages']['p2']['original_evidence']
    assert all(r['result']['decision']=='approved' and r['receipt'] for r in out.values())
    assert len({r['receipt']['ref'] for r in out.values()})==2

def test_partial_success_only_failed_page_is_resent_with_same_total_allowance(review_system):
    s=review_system;s.mode['value']='partial';out=s.run()
    assert len(s.calls)==2 and list(s.calls[1]['pages'].values())[0]['review_page_id']=='page-2'
    with operation():rows=[Snapshot(s.root).row(k) for k in Snapshot(s.root).index['pages']]
    assert sorted(len(r['attempts']) for r in rows)==[1,2]
    before={a:r['receipt'] for a,r in out.items()};s.run();assert len(s.calls)==2 and all(before[a]==r['receipt'] for a,r in s.run().items())

def test_negative_semantic_conclusion_is_known_and_never_a_mechanical_retry(review_system):
    s=review_system;s.mode['value']='negative';out=s.run()
    assert len(s.calls)==1 and out['e2']['result']['decision']=='revision_required'
    s.run();assert len(s.calls)==1

def test_saved_response_without_receipt_recovers_without_resending(review_system):
    s=review_system;s.mode['value']='capture-crash'
    with pytest.raises(RuntimeError):s.run()
    s.mode['value']='valid';s.epoch['value']='resumed';out=s.run()
    assert len(s.calls)==1 and len(out)==2 and all(r['receipt'] for r in out.values())

def test_stop_after_one_receipt_preserves_it_and_finishes_other_slot(review_system,monkeypatch):
    s=review_system;original=SplitPageCalls.settle
    def interrupt(self,*args,**kwargs):
        def stop(_):raise RuntimeError('isolated stop after first durable receipt')
        return original(self,*args,after_page=stop,**kwargs)
    monkeypatch.setattr(SplitPageCalls,'settle',interrupt)
    with pytest.raises(RuntimeError):s.run()
    with operation():prior={k:copy.deepcopy(p.get('receipt')) for k,p in Snapshot(s.root).index['pages'].items()}
    monkeypatch.setattr(SplitPageCalls,'settle',original);s.epoch['value']='resumed';s.run()
    with operation():now=Snapshot(s.root).index['pages']
    assert len(s.calls)==1 and all(now[k]['receipt']==p for k,p in prior.items() if p)

def test_changed_page_body_cannot_reset_budget_or_borrow_another_result(review_system):
    s=review_system;s.run();before=(s.root/'page-call-budget.json').read_bytes()
    s.value['views'][1]['content']='Different evidence-bound body'
    with pytest.raises(ContractError,match='differ'):s.run()
    assert len(s.calls)==1 and (s.root/'page-call-budget.json').read_bytes()==before

@pytest.mark.parametrize('partial',[False,True])
def test_full_and_partial_acceptance_use_each_approved_page_receipt(review_system,monkeypatch,partial):
    from hermes_source_units import build_acceptance as acceptance
    from hermes_source_units.review_receipts import verification_scope
    s=review_system;out=s.run();pages=s.value['backend']['pages'];service=NS(vault=s.adapter.workflow.vault)
    monkeypatch.setattr(acceptance,'assess',lambda _s,_r,p,**kw:next(m['assessment'] for m in pages.values() if m['page']['page_id']==p['page_id']))
    with verification_scope():
        for alias,row in list(out.items())[:1 if partial else 2]:
            meta=pages[alias];r={'actor':'operator','note':row['result']['review_note'],
                'acceptance':acceptance.record(meta['assessment'],review_audit_ref=row['audit_ref'],review_receipt=row['receipt'])}
            acceptance.validate_acceptance(service,{'run_id':'isolated-run'},meta['page'],r)
    wrong={'actor':'operator','note':out['e2']['result']['review_note'],
        'acceptance':acceptance.record(pages['e1']['assessment'],review_audit_ref=out['e2']['audit_ref'],review_receipt=out['e2']['receipt'])}
    with pytest.raises(ContractError):acceptance.validate_acceptance(service,{'run_id':'isolated-run'},pages['e1']['page'],wrong)

def test_actual_dispatch_persists_its_prepared_budget_without_reconstructing_it(tmp_path,monkeypatch):
    import fixed_semantic as fixed,citation_amendment_calls
    from fixed_semantic_schema import schema_for
    from input_budget import check_request
    monkeypatch.setattr(citation_amendment_calls,'check_dispatch',lambda *_:None)
    view={'contract':'independent-page-review/v1','pages':{'p1':{'content':'isolated'}},'page_members':{'p1':'isolated'},'image_bindings':{'p1':[]}}
    sc=schema_for('page-review-pair',view);p=fixed.prepare_request_projection('page-review-pair',view,sc,[])
    p['budget_check']=check_request(p,profile('review-40000'),'page-review-pair')
    raw=json.dumps({'results':{'p1':{'decision':'approved','review_note':'isolated dispatch fixture'}}})
    adapter=NS(workflow=NS(vault=tmp_path));binding={'workflow_id':'fixture','node':'build-finalize','task_id':'isolated','template_hash':'fixed'}
    result,ref=fixed._dispatch_model_call(adapter,binding,'page-review-pair',view,
        lambda *_:(raw,{'origin':'isolated_fixture'}),40000,policy=None,attempt_limit=1,prepared=p)
    audit=json.loads((tmp_path/ref).read_text())
    assert result==raw and audit['fixed_input_budget']==p['budget_check'] and audit['limit']==40000
