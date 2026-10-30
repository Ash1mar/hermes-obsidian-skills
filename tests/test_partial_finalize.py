"""Canonical partial commits, crash recovery, source isolation and release gates."""
import copy,json
from contextlib import contextmanager
import pytest
from test_p3_knowledge_build import vault,publish_sources,run_pass,FileKnowledgeBuildService
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.build_acceptance import assess,record
from hermes_source_units import partial_finalize as partial
from hermes_source_units.finalize_revision_state import append,work_id
from hermes_source_units.workflow_guard import _ACTIVE,_BINDING

@contextmanager
def writer(service):
    b={'workflow_id':'fixture','task_id':'native','node':'build-finalize','actor':'agent'}
    t=_BINDING.set(b);a=_ACTIVE.set({'kind':'build-finalize','actor':'agent','vault':str(service.vault),'workflow_id':'fixture'})
    try:yield b
    finally:_ACTIVE.reset(a);_BINDING.reset(t)

@pytest.fixture
def ready(vault):
    service=FileKnowledgeBuildService(vault)
    refs=publish_sources(vault,['# Pump\nSupported fact zero.\n','# Pump\nSupported fact one.\n'])
    pairs=[run_pass(service,'partial-'+str(i),r) for i,r in enumerate(refs)]
    decisions=[]
    for i,(_,p) in enumerate(pairs):
        decisions.append({'candidate_refs':[{'pass_id':p['pass_id'],'candidate_id':'pump'}],
            'identity':{'kind':'entity','identity_key':'pump-'+str(i),'canonical_name':'Pump '+str(i),'aliases':[]},
            'action':'create','path':'30_Cards/partial-'+str(i)+'.md','content':'# Pump '+str(i)+'\nSupported fact.\n'})
    run=service.reduce({'run_id':'partial-run','actor':'agent','tasks':[{'task_id':t['task_id'],'revision':t['revision']} for t,_ in pairs],
        'expected_registry_revision':0,'document_registry_revision':1,'decisions':decisions,'reason':'Isolated partial contract fixture'})['run']
    reviews=[{'page_id':p['page_id'],'authored_sha256':p['authored_sha256'],'parent_authored_sha256':None,
        'actor':'agent','note':'Program acceptance only','acceptance':record(assess(service,run,p))} for p in run['page_revisions']]
    with writer(service) as binding:
        registry=service._registry();snapshot={'binding':binding,'backend':{'runs':[run],'registry':registry}}
        sp=vault/'_system/reports/partial-basis.json';sp.parent.mkdir(parents=True,exist_ok=True);sp.write_bytes(_json_bytes(snapshot))
        snap=partial.pin(vault,sp.relative_to(vault).as_posix())
        items=[{'logical_work':{'run_id':run['run_id'],'page_id':p['page_id']},'state':'ready' if i==0 else 'deferred'} for i,p in enumerate(run['page_revisions'])]
        manifest={'contract':'build-evidence-disposition/v1','binding':binding,'snapshot':snap,
            'evidence_checks':{'ok':True},'items':items,'prepared_reviews':[{'run_id':run['run_id'],**reviews[0]}]}
        mp=vault/'_system/reports/partial-disposition.json';mp.write_bytes(_json_bytes(manifest))
        append(vault,'fixture',{'work_id':work_id(run['run_id'],'*'),'run_id':run['run_id'],'page_id':'*',
            'state':'enabled','run_sha256':fingerprint(run),'grants':[]},'enable','enable-partial',{})
    req={'run_id':run['run_id'],'actor':'agent','expected_revision':1,'reviews':[reviews[0]],
         'partial_manifest':partial.pin(vault,mp.relative_to(vault).as_posix())}
    return service,run,reviews,req

def test_partial_commit_keeps_original_run_gaps_and_source_tasks(ready,vault):
    service,run,reviews,req=ready;original=service._run_path(run['run_id']).read_bytes()
    with writer(service):result=service.finalize(req)
    assert result['newly_committed_page_count']==1 and result['source_tasks_completed'] is False
    assert (vault/run['page_revisions'][0]['path']).exists()
    assert not (vault/run['page_revisions'][1]['path']).exists()
    assert service._run_path(run['run_id']).read_bytes()==original
    assert all(service._task(t['task_id'])['status']=='running' for t in run['tasks'])
    assert len(partial.progress(service,run)['committed'])==1
    assert service.validate_run(run['run_id'])['state']=='draft'
    with pytest.raises(ContractError,match='INCOMPLETE_COVERAGE'):partial.assert_release_ready(service)

def test_cancel_resume_replay_does_not_duplicate_canonical_submission(ready):
    service,run,_,req=ready
    with writer(service):first=service.finalize(req)
    registry=copy.deepcopy(service._registry());before=list(partial.root(service,run).glob('*.intent.json'))
    with writer(service):second=service.finalize(req)
    assert first['newly_committed_page_count']==1 and second['newly_committed_page_count']==0
    assert service._registry()==registry and list(partial.root(service,run).glob('*.intent.json'))==before

def test_each_locked_finalize_reloads_passes_once_and_rejects_changed_evidence(ready,monkeypatch):
    service,run,reviews,req=ready;original=service._pass_index;scans=[]
    def counted(task_ids):
        scans.append(list(task_ids));return original(scans[-1])
    monkeypatch.setattr(service,'_pass_index',counted)
    final={k:v for k,v in req.items() if k!='partial_manifest'};final['reviews']=reviews
    service.finalize(final)
    assert len(scans)==1
    # Reuse is confined to one locked invocation, never persisted across calls.
    from hermes_source_units.build_acceptance import validate_acceptance
    paths=list((service.vault/f"_system/knowledge-builds/task-{run['tasks'][0]['task_id']}/passes").glob('*.json'))
    path=next(p for p in paths if json.loads(p.read_text())['pass_kind']=='citation')
    value=json.loads(path.read_text());value['candidates'][0]['finding']='changed after first Finalize'
    path.write_bytes(_json_bytes(value))
    with pytest.raises(ContractError):validate_acceptance(service,run,run['page_revisions'][0],reviews[0])

def test_crash_after_revision_write_finishes_exact_intent_without_domain_retry(ready,monkeypatch):
    import hermes_source_units.knowledge_build as kb
    service,run,_,req=ready;write=kb._write_atomic;crashed=False
    def interrupted(path,data):
        nonlocal crashed
        write(path,data)
        if 'page-revisions' in path.parts and not crashed:
            crashed=True;raise RuntimeError('isolated crash after durable sidecar')
    monkeypatch.setattr(kb,'_write_atomic',interrupted)
    with writer(service),pytest.raises(RuntimeError):service.finalize(req)
    assert partial.progress(service,run)['pending'] and not list(partial.root(service,run).glob('*.receipt.json'))
    monkeypatch.setattr(kb,'_write_atomic',write)
    with writer(service):done=service.finalize(req)
    assert done['newly_committed_page_count']==1 and not partial.progress(service,run)['pending']
    assert all(service._task(t['task_id'])['status']=='running' for t in run['tasks'])

@pytest.mark.parametrize('change',['actor','manifest','selection','registry','permission'])
def test_unsafe_partial_request_never_writes_a_canonical_page(ready,vault,change):
    service,run,reviews,req=ready;req=copy.deepcopy(req)
    if change=='actor':req['actor']='another'
    elif change=='manifest':req['partial_manifest']['sha256']='0'*64
    elif change=='selection':req['reviews']=[reviews[1]]
    elif change=='registry':
        registry=service._registry();registry['revision']+=1
        (vault/'_system/metadata/knowledge-identities.json').write_bytes(_json_bytes(registry))
    elif change=='permission':
        state=vault/'_system/ledgers/ingest-workflows/fixture/finalize-controls/state.json'
        state.unlink()
    with writer(service),pytest.raises(ContractError):service.finalize(req)
    assert not any((vault/p['path']).exists() for p in run['page_revisions'])

def test_foreground_caller_cannot_use_native_partial_submission(ready,vault):
    service,run,_,req=ready
    with pytest.raises(ContractError,match='ACCESS_DENIED'):service.finalize(req)
    assert not (vault/run['page_revisions'][0]['path']).exists()

def test_full_completion_after_partial_commits_finishes_sources_only_once(ready):
    service,run,reviews,req=ready
    with writer(service):service.finalize(req)
    final={k:v for k,v in req.items() if k!='partial_manifest'};final['reviews']=reviews
    result=service.finalize(final)
    assert result['run']['state']=='completed'
    assert all(service._task(t['task_id'])['status']=='completed' for t in run['tasks'])
    assert service.validate_run(run['run_id'])['state']=='completed'
    assert not service.finalize(final)['committed']
