"""Scoped native closure, exact release isolation, dependency and late-result fences."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from test_partial_finalize import ready,vault,writer
from hermes_source_units import ContractError,FileVaultFinalizeService
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.partial_finalize import pin,progress
from hermes_source_units.finalize_revision_state import load,append,work_id
from hermes_source_units.delivery_scope import CONTRACT,close,verify,guard_page,dependency_partition,closure_refs

@pytest.fixture
def scoped(ready,vault,request):
    k,run,reviews,req=ready
    with writer(k) as binding:k.finalize(req)
    pointer=req['partial_manifest'];original=json.loads((vault/pointer['ref']).read_text())
    basis={'binding':binding,'backend':{'runs':[run],'registry':k._registry(),'pages':{}},'views':[]}
    path=vault/'_system/reports/delivery-basis.json';path.write_bytes(_json_bytes(basis))
    include=run['page_revisions'][0];exclude=run['page_revisions'][1]
    scope={'contract':CONTRACT,'workflow_id':'fixture','actor':'agent','snapshot':pin(vault,path.relative_to(vault).as_posix()),
        'runs':[{'run_id':run['run_id'],'manifest':pin(vault,k._run_path(run['run_id']).relative_to(vault).as_posix()),
            'tasks':[pin(vault,k._task_path(t['task_id']).relative_to(vault).as_posix()) for t in run['tasks']]}],
        'included':[{'run_id':run['run_id'],**{key:include[key] for key in ('page_id','revision_id','authored_sha256','path')}}],
        'excluded':[{'run_id':run['run_id'],'page_id':exclude['page_id'],'path':exclude['path'],'category':'unresolved','reason':'pending attribution','reopen_condition':'verified new evidence'}],
        'coverage_gaps':{'coverage_complete':False,'inherited':[{'original_gap':'retained'}]}}
    scope['scope_id']=fingerprint(scope)
    path=vault/f'_system/ledgers/ingest-workflows/fixture/delivery-scopes/{scope["scope_id"]}.json';path.parent.mkdir(parents=True);path.write_bytes(_json_bytes(scope))
    pointer=pin(vault,path.relative_to(vault).as_posix());state=load(vault,'fixture');work=copy.deepcopy(state['works'][work_id(run['run_id'],'*')]);work['delivery_scope']=pointer
    append(vault,'fixture',work,'delivery-authorize','authorize-delivery',{'actor':'agent'})
    return k,run,binding,basis,pointer,req

def test_native_scope_closure_is_idempotent_and_never_completes_source_tasks(scoped,vault):
    k,run,b,basis,pointer,req=scoped;before=k._run_path(run['run_id']).read_bytes()
    with writer(k):first=close(k,b,basis);again=close(k,b,basis)
    assert first==again and k._run_path(run['run_id']).read_bytes()==before
    value=verify(k,pointer)
    assert value['delivery_complete'] and not value['coverage_complete'] and not value['source_tasks_completed']
    assert all(k._task(t['task_id'])['status']=='running' for t in run['tasks'])
    with pytest.raises(ContractError,match='DELIVERY_SCOPE_EXCLUDED'):guard_page(k,'fixture',run['page_revisions'][1]['path'])
    with pytest.raises(ContractError):close(k,b,basis)

def test_scoped_audit_release_preserves_separate_business_index_conditions(scoped,vault):
    k,run,b,basis,pointer,req=scoped
    with writer(k):close(k,b,basis)
    service=FileVaultFinalizeService(vault)
    request={'release_id':'delivery','actor':'agent','expected_state_revision':0,'build_run_ids':[run['run_id']],
        'delivery_scopes':closure_refs(k,[run['run_id']]),'reason':'scoped delivery'}
    plan=service.plan(request)['plan']
    assert len(plan['subjects'])==1 and len(plan['index_eligibility'])==1 and not plan['blockers']
    assert plan['index_eligibility'][0]['eligible'] is False
    assert 'business-unassessed' in plan['index_eligibility'][0]['reasons'] and 'visibility-draft' in plan['index_eligibility'][0]['reasons']
    assert {p['page_id'] for p in plan['delivery']['excluded']}=={run['page_revisions'][1]['page_id']}
    assert plan['delivery']['raw_source_indexing'] is False
    with pytest.raises(ContractError):service.plan({**request,'release_id':'unbound','delivery_scopes':[]})

def test_scoped_audit_release_applies_exact_scope_once_without_business_promotion(scoped,vault):
    k,run,b,basis,pointer,req=scoped
    with writer(k):close(k,b,basis)
    service=FileVaultFinalizeService(vault)
    plan=service.plan({'release_id':'eligible','actor':'agent','expected_state_revision':0,
        'build_run_ids':[run['run_id']],'delivery_scopes':closure_refs(k,[run['run_id']]),'reason':'eligible delivery'})['plan']
    assert plan['blockers']==[] and len(plan['subjects'])==1 and len(plan['index_eligibility'])==1
    assert all(r['kind']=='knowledge_page' for r in plan['index_eligibility'])
    request={'release_id':'eligible','actor':'agent','plan_id':plan['plan_id'],'expected_revision':1}
    first=service.apply(request);assert first['committed'] and not service.apply(request)['committed']
    assert service.validate('eligible')['ok'] and len(first['release']['delivery']['excluded'])==1
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qmd-like-rag/src'))
    from qmd_like_rag.corpus import load_release_corpus
    corpus=load_release_corpus(vault)
    assert corpus.projections==()
    assert len(progress(k,run)['committed'])==1 and all(k._task(t['task_id'])['status']=='running' for t in run['tasks'])

@pytest.mark.parametrize('change',['scope','canonical','task','closure'])
def test_scope_cannot_mask_changed_evidence(scoped,vault,change):
    k,run,b,basis,pointer,req=scoped
    with writer(k):receipt=close(k,b,basis)
    if change=='scope':(vault/pointer['ref']).write_text('{}')
    elif change=='canonical':(vault/run['page_revisions'][0]['path']).write_text('changed')
    elif change=='task':k._task_path(run['tasks'][0]['task_id']).write_text('{}')
    else:(vault/receipt['ref']).write_text('{}')
    with pytest.raises((ContractError,KeyError)):verify(k,pointer)

def test_dependency_exclusion_is_transitive_and_never_spreads_by_shared_source():
    pages=[{'page_id':str(i),'path':str(i)+'.md','identity':{'canonical_name':str(i),'aliases':[]},
        'content':text} for i,text in enumerate(('bad','[[0]]','[[1]]','independent'))]
    result=dependency_partition(pages,{'0':{'category':'unresolved','reason':'pending'}})
    assert result['included']==['3'] and set(result['excluded'])=={'0','1','2'}

def test_unknown_quarantine_requires_ended_native_owner(tmp_path,monkeypatch):
    from finalize_delivery_control import quarantine
    path=tmp_path/'journal.json';path.write_bytes(_json_bytes({'attempts':[{'task_id':'native','attempt_id':'attempt','wire_sha256':'wire'}]}))
    item={'history':{'journal':pin(tmp_path,'journal.json')},'observed_status':'completion_unknown'}
    state={'task':{'id':'native','status':'archived','worker_pid':123},'runs':[{'id':1,'ended_at':2}]}
    adapter=NS(workflow=NS(vault=tmp_path),kanban=NS(task_snapshot_fresh=lambda *a:state))
    w={'kanban':{'board_id':'board'}}
    with pytest.raises(ContractError):quarantine(adapter,w,item)
    state['task']['worker_pid']=None;state['runs'][0]['ended_at']=None
    with pytest.raises(ContractError):quarantine(adapter,w,item)
    state['runs'][0]['ended_at']=2
    proof=quarantine(adapter,w,item)
    assert proof['local_invocations_stopped'] and proof['remote_completion_certified'] is False

def test_automatic_disposition_never_labels_all_revision_opinions_model_errors():
    from finalize_delivery_control import automatic_dispositions
    rows=automatic_dispositions({'items':[{'state':'deferred','issue_id':str(i),'observed_status':status,'diagnostic':None}
        for i,status in enumerate(('revision_required','explicit_refusal','completion_unknown'))]})
    assert [r['category'] for r in rows]==['unresolved','material_insufficient','unresolved']
    assert all(r['attribution_confirmed'] is False for r in rows)
