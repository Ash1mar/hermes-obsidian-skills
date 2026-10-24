"""Dependency inspection is read-only and never authorizes a consumed rewrite."""
import copy
from types import SimpleNamespace
import pytest
from test_p3_knowledge_build import vault, workflow_request
from test_reduce_execution_recovery import stopped_reducer
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.citation_revision_impact import preview, successor_delta
import citation_revision_preview


def selection(a,w):
    k=a.workflow.knowledge;b=k._batch(w['batch_id'])
    source=k._resource_reduction(b['batch_id'],b['resource_reduction_ids'][0])
    tid=source['tasks'][0]['task_id'];prior=max(k._pass_index([tid])[1][tid],key=lambda p:p['sequence'])
    return {'task_id':tid,'pass_id':prior['pass_id'],
            'candidate_ids':[prior['candidates'][0]['candidate_id']]},prior


def test_inspection_pins_real_consumption_and_cannot_mutate_or_grant(vault,monkeypatch):
    a,w,*_=stopped_reducer(vault,monkeypatch,'global-reduce')
    selected,prior=selection(a,w)
    before={p:p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    req=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        recovery_id='impact-only',recovery_scope='reduce-citation-impact/v1',selections=[selected],
        target_contract_sha256=citation_revision_preview.contract_hash())
    result=a.recover_reduce_execution(req,dry_run=True)
    impact=result['impact']
    assert result['new_model_calls_granted']==0 and not result['resumed']
    assert sum(s['requires_revalidation'] for s in impact['sources'])==1
    assert len(impact['proposals'])==1 and impact['proposals'][0]['selected_candidate_dependency']
    assert not impact['reuse_approved'] and not impact['consumed_revision_execution_supported']
    assert any('/passes/' in p for p in impact['pins'])
    assert before=={p:p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    with pytest.raises(ContractError,match='read-only'):a.recover_reduce_execution(req)
    assert before=={p:p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    selected['pass_id']='f'*64
    with pytest.raises(ContractError,match='current citation'):preview(a.workflow,w,[selected])


def test_successor_changes_cannot_silently_modify_other_claims_or_scope(vault,monkeypatch):
    a,w,*_=stopped_reducer(vault,monkeypatch)
    selected,prior=selection(a,w)
    # A fixture-only second fact exercises same-Pass collateral dependencies.
    prior=copy.deepcopy(prior);other=copy.deepcopy(prior['candidates'][0]);other['candidate_id']='other'
    prior['candidates'].append(other)
    prior['pass_id']=fingerprint({k:v for k,v in prior.items() if k!='pass_id'})
    def successor():
        new=copy.deepcopy(prior);new.update(sequence=prior['sequence']+1,
            supersedes_pass_id=prior['pass_id'],revision_id='native-revision')
        return new
    def seal(new):
        new['pass_id']=fingerprint({k:v for k,v in new.items() if k!='pass_id'});return new
    new=successor();new['candidates'][0]['conditions']=['A verified correction']
    delta=successor_delta(prior,seal(new),selected['candidate_ids'])
    assert delta['domain_affected_candidate_ids']==selected['candidate_ids']
    assert len(delta['identity_affected_candidate_ids'])==2 and not delta['reuse_approved']
    new=successor();new['inspections'][0]['qa_note']='Changed shared limitation'
    assert len(successor_delta(prior,seal(new),selected['candidate_ids'])['domain_affected_candidate_ids'])==2
    new=successor();new['candidates'][1]['finding']='Unselected semantic mutation'
    with pytest.raises(ContractError,match='unselected'):successor_delta(prior,seal(new),selected['candidate_ids'])
    new=successor();new['reading_window_id']='0'*64
    with pytest.raises(ContractError,match='original task'):successor_delta(prior,seal(new),selected['candidate_ids'])


def test_active_global_pages_are_traced_by_canonical_ref_without_approving_reuse(vault):
    from test_p3_knowledge_build import prepare_layered_reduce_batch,resource_reduce_requests
    k,_,citations,tasks=prepare_layered_reduce_batch(vault,'impact-pages')
    requests=resource_reduce_requests('impact-pages',citations,tasks)
    reductions=[k.reduce_resource(r)['reduction'] for r in requests]
    decisions=[{'identity':{'kind':'entity','identity_key':f'page:{i}',
                           'canonical_name':f'Page {i}','aliases':[]},
                'action':'create','path':f'30_Cards/page-{i}.md','content':f'# Page {i}\n',
                'candidate_refs':r['proposals'][0]['candidate_refs']} for i,r in enumerate(requests)]
    k.reduce_global({'batch_id':'impact-pages','actor':'agent',
        'resource_reduction_ids':[r['reduction_id'] for r in reductions],
        'runs':[{'run_id':'run-impact','actor':'agent','tasks':tasks,'expected_registry_revision':0,
                 'document_registry_revision':1,'decisions':decisions,'reason':'isolated dependency fixture'}],
        'omitted_candidate_refs':[]})
    w={'workflow_id':'impact','revision':1,'batch_id':'impact-pages','actor':'agent'}
    selected={'task_id':tasks[0]['task_id'],'pass_id':citations[tasks[0]['task_id']],
              'candidate_ids':['pump-1']}
    before={p:p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    report=preview(SimpleNamespace(vault=vault,knowledge=k),w,[selected])
    assert report['aggregate_requires_revalidation']
    assert [p['selected_candidate_dependency'] for p in report['pages']]==[True,False]
    assert [p['predecessor_identity_dependency'] for p in report['pages']]==[True,False]
    assert all(not p['reuse_approved'] for p in report['pages'])
    assert before=={p:p.read_bytes() for p in vault.rglob('*') if p.is_file()}
