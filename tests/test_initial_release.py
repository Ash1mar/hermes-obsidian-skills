"""First publication binds real Build closure, not an empty draft subject list.

All fixtures are isolated; no production workflow or native model is executed.
"""
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from test_delivery_scope import scoped, ready, vault, writer
from hermes_source_units import ContractError, FileVaultFinalizeService
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.partial_finalize import pin
from hermes_source_units.validation import fingerprint
from hermes_source_units.delivery_scope import close, closure_refs
from hermes_source_units.page_governance import POLICY, create, effective
from hermes_source_units.initial_release import CONTRACT, validate, admitted_pointer
from hermes_source_units.workflow_guard import _ACTIVE, _BINDING


@pytest.fixture(autouse=True)
def material_quality(request,monkeypatch):
    import test_partial_finalize as material
    qa=getattr(request,'param','usable')
    original=material.run_pass
    monkeypatch.setattr(material,'run_pass',lambda *a,**kw:original(*a,**{**kw,'qa':qa}))


def save(vault, ref, value):
    path = vault/ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))
    return pin(vault, ref)


@contextmanager
def planner(vault):
    binding = {'workflow_id':'fixture','task_id':'planner','node':'vault-finalize-plan','actor':'agent'}
    b = _BINDING.set(binding)
    a = _ACTIVE.set({'kind':'vault-finalize-plan','actor':'agent','vault':str(vault),'workflow_id':'fixture'})
    try:
        yield
    finally:
        _ACTIVE.reset(a)
        _BINDING.reset(b)


@pytest.fixture
def initial(scoped, vault, monkeypatch):
    k, run, binding, basis, delivery, req = scoped
    with writer(k):
        close(k, binding, basis)
    service = FileVaultFinalizeService(vault)
    real_eligibility = service._source_eligibility
    with monkeypatch.context() as patch:
        patch.setattr(service,'_source_eligibility',lambda *a:[{**r,'eligible':False} for r in real_eligibility(*a)])
        plan = service.plan({'release_id':'blocked-first','actor':'agent','expected_state_revision':0,
            'build_run_ids':[run['run_id']],'delivery_scopes':closure_refs(k,[run['run_id']])})['plan']
    assert plan['subjects']==[] and plan['blockers']==['no-deliverable-pages']
    return authority(service, run, plan, vault, monkeypatch)


def authority(service, run, plan, vault, monkeypatch):
    source_registry = json.loads((vault/'_system/metadata/document-registry.json').read_text())
    outcomes = [{'resource_id':r['resource_id'],'content_sha256':r['content_sha256'],'status':'ready'}
                for r in source_registry['records']]
    bootstrap = save(vault,'_system/reports/vault-startup/fixture.json',{
        'contract':'vault-startup-report/v1','vault_id':service.source.vault_manifest['vault']['id'],
        'workflow_id':'fixture','source_hashes':{r['resource_id']:r['content_sha256'] for r in outcomes}})
    w = {'workflow_id':'fixture','actor':'agent','scope':{'execution_mode':'auto_full','bootstrap_ref':bootstrap['ref']},
         'source_outcomes':outcomes,'batch_id':'batch','release_recovery_history':[]}
    monkeypatch.setattr(service.build,'_batch',lambda bid:{'run_ids':[run['run_id']]})
    import hermes_source_units.ingest_workflow as wf
    monkeypatch.setattr(wf,'FileIngestWorkflowService',lambda *a:NS(status=lambda wid:w))
    operation = 'initial-source'
    root = f'_system/metadata/source-governance-resolutions/{operation}'
    source_q = {'workflow_id':'fixture','actor':'agent','workflow_ref':'_system/ledgers/ingest-workflows/fixture.json',
        'operation_id':operation,'automatic_approval_activation':True,'resource_ids':[r['resource_id'] for r in outcomes]}
    source_q['input_digest']='sha256:'+fingerprint(source_q)
    txn = save(vault,root+'/transaction.json',{'request':source_q})
    receipt = save(vault,root+'/receipt.json',{'applied':True,'operation_id':operation,'model_calls':0,
        'rows':[{'resource_id':r['resource_id'],'resolved':True} for r in outcomes]})
    blocked = pin(vault,service._plan_path(plan['release_id']).relative_to(vault).as_posix())
    q = {'workflow_id':'fixture','actor':'agent','operation_id':'initial-pages','policy':POLICY,
        'automatic_page_approval':True,'stop_after':'release_apply','retrieval_scope':service.source.vault_manifest['vault']['id'],
        'base_release':blocked,'basis_kind':'initial_draft_plan','bootstrap':bootstrap,
        'source_governance_receipt':receipt,'source_governance_transaction':txn,'human_review_performed':False}
    q['input_digest']='sha256:'+fingerprint(q)
    grant = save(vault,'_system/reports/initial-grant.json',{'contract':CONTRACT,'request':q,'new_model_calls':0})
    recovery_q = {'workflow_id':'fixture','actor':'agent','recovery_id':'initial','recovery_scope':'governance-resolved/v1',
        'initial_page_governance':grant}
    recovery_q['input_digest']='sha256:'+fingerprint(recovery_q)
    report = save(vault,'_system/reports/initial-recovery.json',{'request':recovery_q,'blocked_plan':blocked,
        'governance_receipt':receipt,'domain_calls_granted':0})
    w['release_recovery_history']=[{'recovery_id':'initial','input_digest':recovery_q['input_digest'],
        'report_ref':report['ref'],'report_sha256':report['sha256']}]
    return service,w,grant,plan,run


@pytest.fixture
def completed_initial(ready, vault, monkeypatch):
    k, run, reviews, req = ready
    final = {key:value for key,value in req.items() if key != 'partial_manifest'}
    final['reviews'] = reviews
    result = k.finalize(final)
    service = FileVaultFinalizeService(vault)
    plan = service.plan({'release_id':'completed-first','actor':'agent',
        'expected_state_revision':0,'build_run_ids':[run['run_id']]})['plan']
    assert plan['subjects'] and plan['blockers'] == [] and not plan.get('delivery_scopes')
    return authority(service,result['run'],plan,vault,monkeypatch)


def test_completed_native_build_qualifies_without_partial_closure(completed_initial):
    service,w,grant,base,run = completed_initial
    before = {p:p.read_bytes() for p in service.vault.rglob('*') if p.is_file()}
    with planner(service.vault):
        pointer = create(service,grant)
        assert create(service,grant) == pointer
    record = json.loads((service.vault/pointer['ref']).read_text())
    assert len(record['pages']) == 2 and all(r['eligible'] for r in record['pages'])
    assert record['new_model_calls'] == 0
    plan = service.plan({'release_id':'qualified-completed','actor':'agent',
        'expected_state_revision':0,'build_run_ids':[run['run_id']],
        'page_governance':pointer})['plan']
    pages = [r for r in plan['index_eligibility'] if r['kind'] == 'knowledge_page']
    assert len(pages) == 2 and all(r['eligible'] for r in pages)
    assert plan['delivery']['raw_source_indexing'] is False
    assert all(p.read_bytes() == raw for p,raw in before.items())
    service.apply({'release_id':plan['release_id'],'actor':'agent','plan_id':plan['plan_id'],
        'expected_revision':plan['revision']})
    assert service.validate(plan['release_id'])['ok']


@pytest.mark.parametrize('change',['other-blocker','eligible','sidecar','run-revision','body'])
def test_completed_initial_rejects_changed_or_already_qualified_input(completed_initial,change):
    from hermes_source_units.initial_release import unqualified_plan
    service,w,grant,base,run = completed_initial
    if change in ('other-blocker','eligible'):
        altered = copy.deepcopy(base)
        if change == 'other-blocker': altered['blockers'] = ['unrelated-blocker']
        else:
            next(r for r in altered['index_eligibility'] if r['kind']=='knowledge_page')['eligible'] = True
        assert not unqualified_plan(altered)
        return
    if change == 'run-revision':
        run['revision'] += 1
        service.build._run_path(run['run_id']).write_bytes(_json_bytes(run))
    elif change == 'sidecar':
        page = run['page_revisions'][0]
        p = service.vault/f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['revision_id']}.json"
        value=json.loads(p.read_text());value['authored_sha256']='0'*64;p.write_bytes(_json_bytes(value))
    else: (service.vault/run['page_revisions'][0]['path']).write_text('changed')
    with planner(service.vault),pytest.raises(ContractError):create(service,grant)


def test_first_plan_qualifies_exact_closed_page_and_leaves_originals_unchanged(initial):
    service,w,grant,blocked,run=initial
    before = {p:p.read_bytes() for p in service.vault.rglob('*') if p.is_file()}
    with planner(service.vault):
        governance=create(service,grant)
        assert create(service,grant)==governance
    value=json.loads((service.vault/governance['ref']).read_text())
    assert value['new_model_calls']==0 and len(value['pages'])==1 and value['pages'][0]['eligible']
    assert value['base_release']==grant_request(service,grant)['base_release']
    page=service._page_revision(value['pages'][0]['page_id'],value['pages'][0]['revision_id'])
    assert page['business_status']=='unassessed' and page['visibility']=='draft'
    assert effective(service,page,governance)=={'qa_status':'usable','business_status':'approved','visibility':'published'}
    plan=service.plan({'release_id':'first-qualified','actor':'agent','expected_state_revision':0,
        'build_run_ids':[run['run_id']],'delivery_scopes':blocked['delivery_scopes'],'page_governance':governance})['plan']
    assert not plan['blockers'] and len(plan['subjects'])==1 and plan['index_eligibility'][0]['eligible']
    assert plan['delivery']['excluded'] and plan['delivery']['coverage_complete'] is False
    assert all(p.read_bytes()==raw for p,raw in before.items())


def grant_request(service,grant):
    return json.loads((service.vault/grant['ref']).read_text())['request']


@pytest.mark.parametrize('change',['vault','bootstrap','source','plan','transaction','qa-policy','human','published'])
def test_initial_authority_rejects_scope_or_evidence_changes(initial,change,monkeypatch):
    service,w,pointer,plan,run=initial
    grant=json.loads((service.vault/pointer['ref']).read_text());q=grant['request']
    if change=='vault':q['retrieval_scope']='another-vault'
    elif change=='bootstrap':w=copy.deepcopy(w);w['scope']['bootstrap_ref']='other.json'
    elif change=='source':w=copy.deepcopy(w);w['source_outcomes'][0]['content_sha256']='0'*64
    elif change=='plan':q['base_release']={'ref':'other.json','sha256':'0'*64}
    elif change=='transaction':(service.vault/q['source_governance_transaction']['ref']).write_text('{}')
    elif change=='qa-policy':q['policy']={**POLICY,'qa':'clear all QA'}
    elif change=='human':q['human_review_performed']=True
    elif change=='published':monkeypatch.setattr(service,'_state',lambda:{'current_release_id':'already-applied'})
    q['input_digest']='sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})
    pointer=save(service.vault,pointer['ref'],grant)
    with pytest.raises((ContractError,FileNotFoundError)):
        validate(service,w,pointer)


def test_grant_must_be_admitted_and_sealed(initial):
    service,w,grant,_,_=initial
    assert admitted_pointer(service.vault,w)==grant
    entry=w['release_recovery_history'].pop()
    with planner(service.vault),pytest.raises(ContractError,match='admitted recovery'):
        create(service,grant)
    w['release_recovery_history'].append(entry)
    (service.vault/entry['report_ref']).write_text('{}')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):
        admitted_pointer(service.vault,w)


def test_source_ineligibility_isolates_page_without_rechecking_a_model(initial,monkeypatch):
    service,w,grant,_,_=initial
    original=service._source_eligibility
    monkeypatch.setattr(service,'_source_eligibility',lambda *a:[{**r,'eligible':False} for r in original(*a)])
    with planner(service.vault):pointer=create(service,grant)
    record=json.loads((service.vault/pointer['ref']).read_text())
    assert record['new_model_calls']==0
    assert all(not p['eligible'] and 'source-governance-ineligible' in p['reasons'] for p in record['pages'])


@pytest.mark.parametrize('material_quality',['needs-qa'],indirect=True)
def test_initial_qualification_preserves_qa_and_excludes_it_without_another_model(initial,material_quality):
    service,w,grant,blocked,run=initial
    with planner(service.vault):pointer=create(service,grant)
    record=json.loads((service.vault/pointer['ref']).read_text())
    assert record['new_model_calls']==0 and record['pages']
    for row in record['pages']:
        assert not row['eligible'] and row['qa_status']=='qa_required' and 'qa-required' in row['reasons']
        assert row['qa_issues']
        page=service._page_revision(row['page_id'],row['revision_id'])
        assert effective(service,page,pointer)=={'qa_status':'qa_required','business_status':'unassessed','visibility':'draft'}
    plan=service.plan({'release_id':'qa-isolated','actor':'agent','expected_state_revision':0,
        'build_run_ids':[run['run_id']],'delivery_scopes':blocked['delivery_scopes'],'page_governance':pointer})['plan']
    assert plan['subjects']==[] and plan['blockers']==['no-deliverable-pages']
