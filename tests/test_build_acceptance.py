"""Isolated policy/commit regression; no native domain work or production writes."""
import copy
import json
from types import SimpleNamespace as NS

import pytest
from test_p3_knowledge_build import vault, publish_sources, run_pass, FileKnowledgeBuildService
from hermes_source_units import ContractError
from hermes_source_units.build_acceptance import assess, record, validate_acceptance
from hermes_source_units.validation import validate_record
import fixed_review
import semantic_output


def draft(vault, *, count=1, qa='usable'):
    service=FileKnowledgeBuildService(vault)
    refs=publish_sources(vault, ['# Pump\nSupported fact '+str(n)+'.\n' for n in range(count)])
    pairs=[run_pass(service, 'task-'+str(n), ref, qa=qa) for n,ref in enumerate(refs)]
    result=service.reduce({'run_id':'program-build','actor':'agent',
        'tasks':[{'task_id':t['task_id'],'revision':t['revision']} for t,_ in pairs],
        'expected_registry_revision':0,'document_registry_revision':1,
        'decisions':[{'candidate_refs':[{'pass_id':p['pass_id'],'candidate_id':'pump'} for _,p in pairs],
            'identity':{'kind':'entity','identity_key':'pump','canonical_name':'Pump','aliases':[]},
            'action':'create','path':'30_Cards/pump.md','content':'# Pump\nSupported fact.\n'}],
        'reason':'Isolated acceptance fixture'})
    run=result['run'];return service,run,run['page_revisions'][0]


def request(run,page,acceptance):
    return {'run_id':run['run_id'],'actor':run['actor'],'expected_revision':run['revision'],
        'reviews':[{'page_id':page['page_id'],'authored_sha256':page['authored_sha256'],
            'parent_authored_sha256':None,'actor':run['actor'],'note':'Program checks; no independent review.',
            'acceptance':acceptance}]}


def test_program_commit_records_unreviewed_and_preserves_publication_gates(vault):
    service,run,page=draft(vault,qa='needs-qa')
    assessment=assess(service,run,page)
    assert assessment['risk_reasons']==[]  # QA alone never starts conversion/model correction.
    result=service.finalize(request(run,page,record(assessment)))
    committed=result['run']['page_revisions'][0]
    assert committed['review']['acceptance']['independent_review']=='not_performed'
    assert (committed['qa_status'],committed['business_status'],committed['visibility'])==('qa_required','unassessed','draft')
    assert service.validate_run(run['run_id'])['ok']
    assert service.finalize(request(run,page,record(assessment)))['committed'] is False


def test_named_risk_cannot_be_downgraded_to_program_acceptance(vault):
    service,run,page=draft(vault,count=2)
    assessment=assess(service,run,page)
    assert 'cross_source_merge' in assessment['risk_reasons']
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match='triggered review'):
        service.finalize(request(run,page,record(assessment)))
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    forged=record(assessment);forged['risk_reasons']=[]
    with pytest.raises(ContractError,match='risk or evidence changed'):
        validate_acceptance(service,run,page,{'acceptance':forged})


def test_changed_draft_and_forged_review_are_not_accepted(vault):
    service,run,page=draft(vault)
    assessment=assess(service,run,page)
    fake=record(assessment,review_audit_ref='_system/fake.json')
    (vault/'_system/fake.json').write_text(json.dumps({'phase':'page-write','model':{'origin':'isolated_fixture'}}))
    with pytest.raises(ContractError,match='native page-review'):
        validate_acceptance(service,run,page,{'acceptance':fake})
    path=vault/f"_system/knowledge-builds/{run['run_id']}/pages/{page['page_id']}.md"
    path.write_text('Changed bytes')
    with pytest.raises(ContractError,match='draft changed'):
        service.finalize(request(run,page,record(assessment)))


def test_schema_does_not_allow_claiming_approval_without_state(vault):
    service,run,page=draft(vault)
    acceptance=record(assess(service,run,page));acceptance['independent_review']='skipped_and_approved'
    with pytest.raises(ContractError):validate_record('build_acceptance',acceptance)


def execution(tmp_path,monkeypatch,risks):
    binding={'workflow_id':'fixture','node':'build-finalize','task_id':'owner'}
    views=[{'page':'e'+str(n),'content':'body-'+str(n)} for n in range(len(risks))]
    pages={v['page']:{'run_id':'run','page':{'page_id':v['page'],'authored_sha256':'a'*64},
        'parent_hash':None,'assessment':{'policy':'explicit-page-risks/v1','risk_reasons':r,'evidence_sha256':'b'*64}}
        for v,r in zip(views,risks)}
    value={'input_id':'c'*64,'views':views,'backend':{'pages':pages,'actor':'agent',
        'batch_id':'batch','runs':[{'run_id':'run','revision':1}]}}
    submitted=[]
    adapter=NS(worker_begin=lambda *_:None,worker_complete=lambda *_:{'ok':True},
        workflow=NS(vault=tmp_path,knowledge=NS(finalize_batch=lambda req:submitted.append(req) or {'ok':True})))
    monkeypatch.setattr(fixed_review,'policy_enabled',lambda *_:True)
    monkeypatch.setattr(fixed_review,'snapshot',lambda *_:copy.deepcopy(value))
    from contextlib import nullcontext
    monkeypatch.setattr(fixed_review,'worker_binding',lambda *_:nullcontext())
    monkeypatch.setattr(fixed_review,'workflow_write_guard',lambda *_a,**_k:nullcontext())
    return adapter,binding,views,submitted


def test_default_program_acceptance_makes_zero_review_calls(tmp_path,monkeypatch):
    a,b,_,submitted=execution(tmp_path,monkeypatch,[[],[]])
    monkeypatch.setattr(semantic_output,'execute',lambda *_a,**_k:pytest.fail('unexpected model review'))
    assert fixed_review.execute(a,b,object(),None,lambda *_:None)['ok']
    reviews=submitted[0]['finalizations'][0]['reviews']
    assert all(r['acceptance']['independent_review']=='not_performed' for r in reviews)


def test_only_named_risk_page_is_reviewed_and_receipt_is_retained(tmp_path,monkeypatch):
    a,b,_,submitted=execution(tmp_path,monkeypatch,[[],['different_applicability']]);calls=[]
    def checked(_a,_b,phase,view,*args,**kwargs):
        calls.append(view)
        return {'decision':'approved','review_note':'Scope distinctions preserved'},'native-review',None
    monkeypatch.setattr(semantic_output,'execute',checked)
    assert fixed_review.execute(a,b,object(),None,lambda *_:None)['ok']
    assert len(calls)==1 and calls[0]['page']=='e1' and calls[0]['review_scope']['risk_reasons']==['different_applicability']
    assert submitted[0]['finalizations'][0]['reviews'][1]['acceptance']['review_audit_ref']=='native-review'


@pytest.mark.parametrize('outcome',['approved','revision_required','unknown'])
def test_existing_review_journal_is_never_discarded_by_program_policy(tmp_path,monkeypatch,outcome):
    a,b,views,submitted=execution(tmp_path,monkeypatch,[[]]);calls=[]
    path=semantic_output.path_for(a,b,'page-review',views[0]);path.parent.mkdir(parents=True)
    path.write_text('{}')  # The real finite executor authenticates/rejects this journal.
    def checked(*args,**kwargs):
        calls.append(args[3]);assert 'review_scope' not in args[3]
        if outcome=='unknown':raise ContractError('SEMANTIC_OUTPUT_HELD','$','uncertain completion')
        return {'decision':outcome,'review_note':'Saved native review'},'old-review',None
    monkeypatch.setattr(semantic_output,'execute',checked)
    if outcome=='approved':
        assert fixed_review.execute(a,b,object(),None,lambda *_:None)['ok']
        assert submitted[0]['finalizations'][0]['reviews'][0]['acceptance']['independent_review']=='approved'
    else:
        with pytest.raises(ContractError):fixed_review.execute(a,b,object(),None,lambda *_:None)
        assert not submitted
    assert len(calls)==1 and path.read_text()=='{}'


def test_old_template_does_not_silently_adopt_new_policy():
    binding={'workflow_id':'old'}
    a=NS(workflow=NS(status=lambda _: {},pinned_templates=lambda _:{'build-finalize':'<!-- hermes-fixed-semantic/v1 -->'}))
    assert fixed_review.policy_enabled(a,binding) is False


def test_update_loss_is_a_local_risk_but_addition_is_not(vault):
    service,run,page=draft(vault)
    first=service.finalize(request(run,page,record(assess(service,run,page))))['run']['page_revisions'][0]
    ref=first['support_refs'][0]
    task,citation=run_pass(service,'update-task',ref)
    identity=run['decisions'][0]['identity']
    def update(content):
        return service.reduce({'run_id':'update-build','actor':'agent',
            'tasks':[{'task_id':task['task_id'],'revision':task['revision']}],
            'expected_registry_revision':1,'document_registry_revision':1,
            'decisions':[{'candidate_refs':[{'pass_id':citation['pass_id'],'candidate_id':'pump'}],
                'identity':identity,'action':'update','path':first['path'],'content':content}],
            'reason':'Isolated update'})['run']
    changed=update('# Pump\nSupported fact.\nAdditional supported fact.\n')
    assert assess(service,changed,changed['page_revisions'][0])['risk_reasons']==[]
    path=vault/f"_system/knowledge-builds/{changed['run_id']}/pages/{changed['page_revisions'][0]['page_id']}.md"
    # Deliberate drift is a hard hold, never a reason to purchase a model opinion.
    path.write_text('# Pump\nMissing old content.\n')
    with pytest.raises(ContractError,match='draft changed'):
        assess(service,changed,changed['page_revisions'][0])


def test_revised_pass_identity_alone_does_not_expand_review_scope(vault):
    from hermes_source_units.build_acceptance import _revised
    from hermes_source_units.validation import fingerprint
    service,run,page=draft(vault)
    candidates,_=service._pass_index([t['task_id'] for t in run['tasks']])
    original,candidate=next(iter(candidates.values()))
    successor=copy.deepcopy(original);successor['supersedes_pass_id']=original['pass_id']
    assert _revised(service,successor,candidate) is False
    changed=copy.deepcopy(candidate);changed['conditions']=['A corrected condition']
    assert _revised(service,successor,changed) is True
    successor['inspections'][0]['qa_note']='Changed shared limitation'
    assert _revised(service,successor,candidate) is True
