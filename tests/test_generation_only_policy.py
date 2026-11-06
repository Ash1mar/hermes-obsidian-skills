"""No model calls: policy immutability, actual core commits and retained gates."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint,validate_record
from hermes_source_units.source_units import _write_atomic,_json_bytes
from hermes_source_units.generation_policy import POLICY,select,capture,validate_run,assert_model_phase,new_run
from hermes_source_units.build_acceptance import assess,record,validate_acceptance
from test_p3_knowledge_build import vault
from test_build_acceptance import draft,request,execution

def pin(vault,actor='agent'):
    w={'workflow_id':'new-test','actor':actor,'scope':{'source_paths':[],'knowledge_selector':'all-current'}}
    identity=vault/'_system/vault.json';v=json.loads(identity.read_text());v['vault']['security_domain']='isolated-test';identity.write_text(json.dumps(v))
    p=vault/'_system/metadata/retrieval-policy.json';r=json.loads(p.read_text());r['query_enabled']=False;p.write_text(json.dumps(r))
    root=vault/'_system/ledgers/ingest-workflows';path=root/'new-test/presentation-contract.json'
    presentation={'workflow_id':'new-test','scope_sha256':fingerprint(w['scope']),'page_semantics_policy':POLICY}
    _write_atomic(path,_json_bytes(presentation));_write_atomic(root/'new-test.json',_json_bytes(w))
    return w,path

def test_default_is_disabled_only_for_new_query_disabled_test_scope(vault):
    w,path=pin(vault);assert select(vault,w)==POLICY
    enabled=copy.deepcopy(w);enabled['scope']['execution_policy']={'independent_page_review':'enabled'};assert select(vault,enabled) is None
    identity=vault/'_system/vault.json';v=json.loads(identity.read_text());v['vault']['security_domain']='production';identity.write_text(json.dumps(v))
    assert select(vault,w) is None
    w['scope']['execution_policy']={'independent_page_review':'disabled'}
    with pytest.raises(ContractError):select(vault,w)

def test_actual_core_finalize_keeps_named_risks_qa_and_unreviewed_state(vault):
    service,run,page=draft(vault,count=2,qa='needs-qa');w,path=pin(vault,run['actor'])
    from hermes_source_units.workflow_guard import _ACTIVE
    token=_ACTIVE.set({'kind':'global-reduce','workflow_id':w['workflow_id']})
    try:pointer=new_run(service,run['actor'])
    finally:_ACTIVE.reset(token)
    run['page_review_policy']=pointer;_write_atomic(service._run_path(run['run_id']),_json_bytes(run));validate_record('build_run',run)
    assessment=assess(service,run,page);assert assessment['policy']==POLICY and 'cross_source_merge' in assessment['risk_reasons']
    a=record(assessment);result=service.finalize(request(run,page,a));p=result['run']['page_revisions'][0]
    assert p['review']['acceptance']['independent_review']=='not_performed'
    assert p['review']['acceptance']['review_audit_ref'] is None
    assert (p['qa_status'],p['business_status'],p['visibility'])==('qa_required','unassessed','draft')
    assert service.validate_run(run['run_id'])['ok']
    # Policy proof remains usable after the mutable workflow pin is replaced.
    path.write_text('{}');assert validate_run(service,result['run'])['contract']==POLICY

def test_forged_policy_cannot_skip_old_risk_review(vault):
    service,run,page=draft(vault,count=2);assessment=assess(service,run,page);assessment['policy']=POLICY
    with pytest.raises(ContractError):validate_acceptance(service,run,page,{'acceptance':record(assessment)})
    w,_=pin(vault,run['actor']);run['page_review_policy']=capture(vault,w,run['actor'])
    with pytest.raises(ContractError):validate_acceptance(service,run,page,{'note':'silent approval'})
    accepted=record(assess(service,run,page));accepted['independent_review']='approved';accepted['review_audit_ref']='old-review'
    with pytest.raises(ContractError):validate_acceptance(service,run,page,{'acceptance':accepted})
    p=vault/run['page_review_policy']['ref'];p.write_text('{}')
    with pytest.raises(ContractError):validate_run(service,run)

def test_build_makes_zero_calls_for_all_named_risks_and_refuses_history_reset(tmp_path,monkeypatch):
    import fixed_review,semantic_output
    a,b,views,submitted=execution(tmp_path,monkeypatch,[['different_applicability'],['claim_variants']])
    from hermes_source_units import generation_policy
    monkeypatch.setattr(generation_policy,'pinned',lambda *_:POLICY)
    original=fixed_review.snapshot
    def snapshot(*args):
        value=original(*args)
        for p in value['backend']['pages'].values():p['assessment']['policy']=POLICY
        return value
    monkeypatch.setattr(fixed_review,'snapshot',snapshot)
    monkeypatch.setattr(semantic_output,'execute',lambda *_a,**_k:pytest.fail('independent model review forbidden'))
    assert fixed_review.execute(a,b,object(),None,lambda *_:None)['ok']
    reviews=submitted[0]['finalizations'][0]['reviews'];assert len(reviews)==2
    assert all(r['acceptance']['independent_review']=='not_performed' and r['acceptance']['risk_reasons'] for r in reviews)
    path=semantic_output.path_for(a,b,'page-review',views[0]);path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{"attempts":[{"state":"unknown"}]}')
    with pytest.raises(ContractError):fixed_review.execute(a,b,object(),None,lambda *_:None)

def test_native_dispatch_is_fenced_before_a_request_can_be_sent(vault):
    import fixed_semantic
    pin(vault);binding={'workflow_id':'new-test'};adapter=NS(workflow=NS(vault=vault))
    for phase in ('page-review','page-review-pair'):
        with pytest.raises(ContractError,match='INDEPENDENT_PAGE_REVIEW_DISABLED'):
            fixed_semantic._dispatch_model_call(adapter,binding,phase,{},lambda *_:pytest.fail('model called'),40000,prepared={})
    assert_model_phase(vault,binding,'page-pair')

def test_only_unconsumed_presentation_gets_disabled_default(vault):
    from hermes_source_units.presentation_contract import pin as pin_presentation
    w,_=pin(vault);w['workflow_id']='untouched-test';service=NS(vault=vault)
    p=pin_presentation(service,w)
    assert p['page_semantics_policy']==POLICY and p['review_risk_contract']==POLICY
    assert 'review_call_contract' not in p
    # A new environment/default cannot reinterpret a saved immutable pin.
    identity=vault/'_system/vault.json';v=json.loads(identity.read_text());v['vault']['security_domain']='production';identity.write_text(json.dumps(v))
    assert pin_presentation(service,w)==p
