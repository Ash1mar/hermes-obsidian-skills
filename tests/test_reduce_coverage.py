"""Isolated scope boundaries; fixture outputs never enter the real workflow."""
import copy
import hashlib
import json
from types import SimpleNamespace
import pytest
from hermes_source_units import ContractError
from hermes_source_units.reduce_coverage import active,check_decisions,check_run,check_request
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.workflow_guard import worker_binding


def fixture(tmp_path):
    def save(ref,value):
        path=tmp_path/ref;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(_json_bytes(value))
        return hashlib.sha256(path.read_bytes()).hexdigest()
    w={'workflow_id':'ingest-scope','actor':'agent','batch_id':'batch','execution_recovery_history':[]}
    manifest={'workflow_id':w['workflow_id'],'allowed_pages':[], 'deferred_candidate_occurrences':2,'held_page_count':1}
    digest=save('manifest.json',manifest)
    approval={'actor':'agent','manifest_sha256':digest,'accept_necessary_coverage_gaps':True}
    approval_sha=save('approval.json',approval)
    identity={'kind':'entity','identity_key':'kept','canonical_name':'Kept','aliases':[]}
    content='# Kept\n';page={'path':'30_Cards/kept.md','identity':identity,
        'candidate_refs':[{'pass_id':'pass','candidate_id':'kept'}],
        'submitted_content_sha256':hashlib.sha256(content.encode()).hexdigest(),
        'authored_content_sha256':hashlib.sha256(content.encode()).hexdigest()}
    proof={'manifest_ref':'manifest.json','manifest_sha256':digest,'approval_ref':'approval.json',
        'approval_sha256':approval_sha,'pages':[page],'new_model_calls_granted':0,
        'omitted_candidate_refs':[{'pass_id':'pass','candidate_id':'gap'}],
        'run_tasks':[{'task_id':'owned','revision':1}],'outside_page_scope_task_ids':['outside'],
        'outside_scope_required_inspection_gaps':[],'partial_task_ids':[],'task_coverage_gaps':[]}
    request={'recovery_scope':'reduce-coverage/v1','workflow_id':w['workflow_id'],'actor':'agent','input_digest':'d'}
    report={'request':request,'batch_id':'batch','entries':[{'coverage_admission':proof}]}
    report_sha=save('admission.json',report)
    w['execution_recovery_history'].append({'report_ref':'admission.json','report_sha256':report_sha,'input_digest':'d'})
    scope=active(tmp_path,w)
    return SimpleNamespace(save=save,w=w,scope=scope,page=page,content=content,root=tmp_path)


@pytest.mark.parametrize('mutation',['extra','missing','candidate','identity','bytes'])
def test_scope_rejects_page_or_claim_expansion(tmp_path,mutation):
    c=fixture(tmp_path);decisions=[{**c.page,'content':c.content}]
    if mutation=='extra':decisions.append(copy.deepcopy(decisions[0]))
    elif mutation=='missing':decisions=[]
    elif mutation=='candidate':decisions[0]['candidate_refs']=[{'pass_id':'pass','candidate_id':'gap'}]
    elif mutation=='identity':decisions[0]['identity']={**c.page['identity'],'identity_key':'gap'}
    else:decisions[0]['content']+='unapproved'
    with pytest.raises(ContractError):check_decisions(c.scope,decisions)


def test_exact_scope_requires_bound_native_worker_and_exact_gaps(tmp_path,monkeypatch):
    c=fixture(tmp_path)
    service=SimpleNamespace(vault=tmp_path)
    import hermes_source_units.reduce_coverage as coverage
    monkeypatch.setattr(coverage,'resolve',lambda *_:(c.scope,c.w))
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService
    monkeypatch.setattr(FileIngestWorkflowService,'__init__',lambda *_:None)
    monkeypatch.setattr(FileIngestWorkflowService,'status',lambda *_:c.w)
    run={'actor':'agent','coverage_scope':c.scope['ref'],'decisions':[{**c.page,'content':c.content}],
         'tasks':c.scope['proof']['run_tasks']}
    req={'batch_id':'batch','actor':'agent','coverage_scope':c.scope['ref'], 'runs':[run],
         'omitted_candidate_refs':c.scope['proof']['omitted_candidate_refs']}
    with pytest.raises(ContractError):check_request(service,req,global_request=True)
    binding={'workflow_id':c.w['workflow_id'],'task_id':'native','node':'global-reduce'}
    with worker_binding(binding):
        assert check_request(service,req,global_request=True)==c.scope['ref']
        bad=copy.deepcopy(req);bad['omitted_candidate_refs']=[]
        with pytest.raises(ContractError):check_request(service,bad,global_request=True)
        bad=copy.deepcopy(req);bad.pop('coverage_scope')
        with pytest.raises(ContractError):check_request(service,bad,global_request=True)


def test_changed_approval_and_withdrawal_fail_closed(tmp_path):
    c=fixture(tmp_path)
    c.save('approval.json',{'accept_necessary_coverage_gaps':False})
    with pytest.raises(ContractError):active(tmp_path,c.w)


def test_downstream_run_carries_incomplete_outcome_and_blocks_changed_draft(tmp_path,monkeypatch):
    c=fixture(tmp_path)
    import hermes_source_units.reduce_coverage as coverage
    monkeypatch.setattr(coverage,'resolve',lambda *_:(c.scope,c.w))
    service=SimpleNamespace(vault=tmp_path,_batch=lambda _: {'run_ids':['run']})
    run={'run_id':'run','actor':'agent','coverage_scope':c.scope['ref'],'decisions':[c.page],
         'tasks':c.scope['proof']['run_tasks'],
         'page_revisions':[{'path':c.page['path'],'authored_sha256':c.page['authored_content_sha256']}]}
    assert check_run(service,run)['coverage_complete'] is False
    run['page_revisions'][0]['authored_sha256']='0'*64
    with pytest.raises(ContractError):check_run(service,run)


def test_scoped_dispatch_cannot_fall_back_to_new_semantic_call(tmp_path,monkeypatch):
    import fixed_semantic,reduce_coverage
    monkeypatch.setattr(reduce_coverage,'current',lambda *_:{'ref':'approved'})
    with pytest.raises(ContractError,match='no fresh semantic calls'):
        fixed_semantic.model_call(SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path)),
            {'workflow_id':'ingest-scope','node':'global-reduce'},'page-write',{},None,30000)


def test_accepted_reduce_revision_no_longer_owns_checkpoint_pause():
    from hermes_source_units.reduce_dependency_revision import scoped
    assert scoped({'pass_revision':{'revision_scope':'reduce-review/v1','state':'awaiting_review'}})
    assert not scoped({'pass_revision':{'revision_scope':'reduce-review/v1','state':'accepted'}})


def test_reopen_preserves_domain_and_pause_history_with_crash_replay(vault,monkeypatch):
    from test_reduce_execution_recovery import stopped_reducer
    from test_p3_knowledge_build import workflow_request
    import reduce_coverage_reopen as reopen
    from hermes_source_units.source_units import _write_atomic
    a,w,card,_,_=stopped_reducer(vault,monkeypatch,'global-reduce')
    scope={'ref':{'exact':'scope'}}
    monkeypatch.setattr(reopen,'active',lambda *_:scope)
    monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',lambda *_a,**_k:None)
    w['current_stage']='checkpoint_1';w['resume_stage']='checkpoint_1'
    w['pause_control']={'boundary':'checkpoint_1','evidence_digest':'sha256:'+'1'*64,
        'report_ref':'_system/test-pause.json','history':[]}
    decision='_system/test-decision.json';(vault/decision).write_text('{"decision":"old"}')
    w['checkpoints']['checkpoint_1']={'state':'approved','approval_digest':'sha256:'+'1'*64,
        'approved_by':'agent','decision_ref':decision,'decision_sha256':hashlib.sha256((vault/decision).read_bytes()).hexdigest()}
    a.workflow._write(w)
    (vault/'_system/test-pause.json').write_text('{"pause":"old"}')
    batch=a.workflow.knowledge._batch(w['batch_id']);batch['run_ids']=['isolated-retired'];batch['global_reduction_id']='1'*64
    _write_atomic(a.workflow.knowledge._batch_path(batch['batch_id']),_json_bytes(batch))
    path=a.workflow.knowledge._run_path('isolated-retired');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'state':'draft','coverage_scope':scope['ref']}))
    monkeypatch.setattr(a.workflow.knowledge,'_global_reduction',lambda *_:{'coverage_scope':scope['ref']})
    # A real ended validator identity is required; fixture board uses an archived card.
    w['kanban']['task_map']=[c for c in w['kanban']['task_map'] if c['node']!='checkpoint-1-validate']
    w['kanban']['task_map'].append({**card,'node':'checkpoint-1-validate'})
    a.workflow._write(w)
    basis='_system/new-cause.json';(vault/basis).write_text('{"cause":"new evidence"}')
    req=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
        recovery_id='isolated-reopen',recovery_scope='reduce-coverage-reopen/v1',reason='New evidence',
        coverage_scope=scope['ref'],basis=[{'ref':basis,'sha256':hashlib.sha256((vault/basis).read_bytes()).hexdigest()}])
    protected={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*') if p.is_file()
               and p!=a.workflow.knowledge._batch_path(batch['batch_id'])}
    before=a.workflow._path(w['workflow_id']).read_bytes()
    assert reopen.recover(a,req,dry_run=True)['applied'] is False
    assert a.workflow._path(w['workflow_id']).read_bytes()==before
    real_write=reopen._write_atomic
    def crash_after_workflow(p,data):
        real_write(p,data)
        if p==a.workflow._path(w['workflow_id']):raise RuntimeError('simulated crash after control write')
    monkeypatch.setattr(reopen,'_write_atomic',crash_after_workflow)
    with pytest.raises(RuntimeError):reopen.recover(a,req)
    assert reopen.pending_path(a.workflow,w['workflow_id']).exists()
    monkeypatch.setattr(reopen,'_write_atomic',real_write)
    assert reopen.recover(a,req)['applied']
    new=a.workflow.status(w['workflow_id'])
    assert new['state']=='cancelled' and new['current_stage']=='reducing' and new['cancel_requested']
    assert new['checkpoints']['checkpoint_1']['state']=='pending'
    assert new['pause_control']['history']==w['pause_control']['history']
    assert (vault/decision).read_text()=='{"decision":"old"}'
    assert (vault/w['pause_control']['report_ref']).read_text()=='{"pause":"old"}'
    from hermes_source_units.reduce_coverage import checkpoint_ref
    assert checkpoint_ref(new,'decisions','checkpoint_1')!=checkpoint_ref(w,'decisions','checkpoint_1')
    assert checkpoint_ref(new,'pauses','checkpoint_1')!=checkpoint_ref(w,'pauses','checkpoint_1')
    report=json.loads((vault/new['execution_recovery_history'][-1]['report_ref']).read_bytes())
    assert report['retired_checkpoint_evidence'][decision]['content']=='{"decision":"old"}'
    assert all(p.read_bytes()==raw for p,raw in protected.items())
    assert reopen.recover(a,req)['replayed']
    assert not reopen.pending_path(a.workflow,w['workflow_id']).exists()


from test_p3_knowledge_build import vault


@pytest.mark.parametrize('partial_source',[False,True])
def test_scoped_global_validate_and_finalize_leave_unowned_tasks_unfinished(vault,tmp_path,monkeypatch,partial_source):
    from test_p3_knowledge_build import prepare_layered_reduce_batch,resource_reduce_requests
    from hermes_source_units.reduce_coverage import task_partition
    import hermes_source_units.reduce_coverage as coverage
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService
    k,_,citations,tasks=prepare_layered_reduce_batch(vault,'scope-integration')
    if partial_source:
        from hermes_source_units.source_units import _write_atomic
        task=k._task(tasks[0]['task_id']);ref=task['inspections'][0]['source_ref']
        unit=next(u for u in k._live_units([ref],'agent',1) if u['ref']==ref['unit_ref'])
        start=unit['locator']['span']['start']
        task['inspections'][0]['source_ref']={**ref,'span':{'start':start,'end':start+1}}
        _write_atomic(k._task_path(task['task_id']),_json_bytes(task))
    requests=resource_reduce_requests('scope-integration',citations,tasks)
    reductions=[k.reduce_resource(r)['reduction'] for r in requests]
    c=fixture(tmp_path/'authority');c.w['batch_id']='scope-integration'
    c.page['candidate_refs']=requests[0]['proposals'][0]['candidate_refs']
    c.scope['proof']['pages']=[c.page]
    c.scope['proof']['omitted_candidate_refs']=requests[1]['proposals'][0]['candidate_refs']
    c.scope['proof'].update(task_partition(k,tasks,[c.page]))
    assert len(c.scope['proof']['run_tasks'])==1 and len(c.scope['proof']['outside_page_scope_task_ids'])==1
    monkeypatch.setattr(coverage,'resolve',lambda *_:(c.scope,c.w))
    monkeypatch.setattr(coverage,'active',lambda *_:c.scope)
    monkeypatch.setattr(FileIngestWorkflowService,'status',lambda *_:c.w)
    run={'run_id':'run-scope-integration','actor':'agent','tasks':c.scope['proof']['run_tasks'],
        'expected_registry_revision':0,'document_registry_revision':1,'coverage_scope':c.scope['ref'],
        'decisions':[{**c.page,'content':c.content,'action':'create'}],'reason':'isolated subset'}
    request={'batch_id':'scope-integration','actor':'agent','resource_reduction_ids':[r['reduction_id'] for r in reductions],
        'runs':[run],'omitted_candidate_refs':c.scope['proof']['omitted_candidate_refs'],'coverage_scope':c.scope['ref']}
    with worker_binding({'workflow_id':c.w['workflow_id'],'task_id':'native','node':'global-reduce'}):
        assert k.reduce_global(request)['ok']
    validation=k.validate_batch('scope-integration')
    assert validation['ok'] and validation['coverage_complete'] is False
    assert validation['uncovered_task_ids']==c.scope['proof']['outside_page_scope_task_ids']
    assert bool(validation['partial_task_ids'])==partial_source
    page=json.loads(k._run_path(run['run_id']).read_bytes())['page_revisions'][0]
    result=k.finalize_batch({'batch_id':'scope-integration','actor':'agent','finalizations':[{
        'run_id':run['run_id'],'actor':'agent','expected_revision':1,'reviews':[{
            'page_id':page['page_id'],'authored_sha256':page['authored_sha256'],
            'parent_authored_sha256':None,'actor':'reviewer','note':'isolated review'}]}]})
    assert result['batch_completed'] and result['coverage_complete'] is False
    outside=c.scope['proof']['outside_page_scope_task_ids'][0]
    assert k._task(outside)['status']=='running'
    assert k._task(outside)['revision']==next(t['revision'] for t in tasks if t['task_id']==outside)
    if partial_source:
        assert k._task(tasks[0]['task_id'])['status']=='running'
        assert k._task(tasks[0]['task_id'])['revision']==tasks[0]['revision']
        assert k.validate_batch('scope-integration')['ok']


def test_task_partition_cannot_isolate_missing_evidence_of_an_adopted_claim(vault,monkeypatch):
    from test_p3_knowledge_build import prepare_layered_reduce_batch,resource_reduce_requests
    from hermes_source_units.reduce_coverage import task_partition
    import hermes_source_units.reading_dependencies as dep
    k,_,citations,tasks=prepare_layered_reduce_batch(vault,'scope-dependency')
    request=resource_reduce_requests('scope-dependency',citations,tasks)[0]
    refs=request['proposals'][0]['candidate_refs']
    monkeypatch.setattr(k,'_existing_reading_package',lambda task,*_:{'task_id':task['task_id']})
    monkeypatch.setattr(dep,'required_reading',lambda *_:([],{}))
    monkeypatch.setattr(dep,'citation_dependency_gaps',lambda source,package,record,supplement:
        [{'task_id':record['task_id']}] if record['task_id']==tasks[1]['task_id'] else [])
    result=task_partition(k,tasks,[{'candidate_refs':refs}])
    assert len(result['outside_scope_required_inspection_gaps'])==1
    monkeypatch.setattr(dep,'citation_dependency_gaps',lambda *_:[{'missing':'required'}])
    with pytest.raises(ContractError,match='allowed page depends'):task_partition(k,tasks,[{'candidate_refs':refs}])


@pytest.mark.parametrize('changed',['journal','body','source','native_audit'])
def test_admitted_proof_reuse_checks_seals_and_preserves_receipt_append(tmp_path,monkeypatch,changed):
    import reduce_coverage as execution
    import fixed_destinations
    from hermes_source_units.validation import fingerprint
    c=fixture(tmp_path)
    source={'contract':'input','view':{},'backend':{}}
    source_sha=c.save('source.json',source)
    c.save('audit.json',{'response_sha256':'native','receipt_refs':[],'validated':True})
    journal_sha=c.save('journal.json',{'attempts':[{'state':'valid'}]})
    (tmp_path/'body.md').write_text(c.content)
    row={**c.page,'draft_ref':'body.md','draft_sha256':hashlib.sha256(c.content.encode()).hexdigest(),
         'storage_destination':c.page['path'],'input_sha256':'0'*64,'audit_ref':'audit.json'}
    c.scope['manifest'].update(allowed_pages=[row],gaps=[],source_snapshot_ref='source.json',source_snapshot_sha256=source_sha)
    c.scope['proof'].update(native_bodies_authenticated=1,finite_journal_seals=[{
        'ref':'journal.json','sha256':journal_sha,'audit_ref':'audit.json',
        'audit_semantic_sha256':fingerprint({'response_sha256':'native'})}])
    monkeypatch.setattr(fixed_destinations,'routes',lambda *_:{row['path']:row['storage_destination']})
    monkeypatch.setattr(fixed_destinations,'compile_content',lambda value,page,dest:(page['content'],[]))
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path))
    assert execution.reuse_pages(adapter,source,c.scope)[0]['content']==c.content
    c.save('audit.json',{'response_sha256':'native','receipt_refs':['new-receipt'],'validated':True})
    assert execution.reuse_pages(adapter,source,c.scope)[0]['content']==c.content
    if changed=='journal':c.save('journal.json',{'attempts':[{'state':'valid'},{'state':'reserved'}]})
    elif changed=='body':(tmp_path/'body.md').write_text('changed')
    elif changed=='source':c.save('source.json',{**source,'view':{'changed':True}})
    else:c.save('audit.json',{'response_sha256':'changed'})
    with pytest.raises(ContractError):execution.reuse_pages(adapter,source,c.scope)


@pytest.mark.parametrize('case',['approved','unauthorized','changed_status'])
def test_batch_partition_preserves_prior_authorized_exclusions(tmp_path,monkeypatch,case):
    import hermes_source_units.reduce_coverage as coverage
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService
    c=fixture(tmp_path)
    monkeypatch.setattr(coverage,'resolve',lambda *_:(c.scope,c.w))
    monkeypatch.setattr(FileIngestWorkflowService,'__init__',lambda *_:None)
    monkeypatch.setattr(FileIngestWorkflowService,'verify_pass_skips',lambda *_:[])
    monkeypatch.setattr(FileIngestWorkflowService,'skipped_pass_tasks',lambda *_:set() if case=='unauthorized' else {'skipped'})
    batch={'batch_id':'batch','global_reduction_id':'global','run_ids':['run'],'task_ids':['owned','outside','skipped']}
    service=SimpleNamespace(vault=tmp_path,_task=lambda _:{'status':'running' if case=='changed_status' else 'skipped'},
        _global_reduction=lambda *_:{'coverage_scope':c.scope['ref'],
            'omitted_candidate_refs':c.scope['proof']['omitted_candidate_refs'],'run_ids':['run']})
    if case!='approved':
        with pytest.raises(ContractError):coverage.batch_outcome(service,batch)
    else:
        result=coverage.batch_outcome(service,batch)
        assert result['previously_excluded_task_ids']==['skipped']
        assert result['coverage_complete'] is False and result['previous_exclusions_completed'] is False


def test_validator_recovery_preserves_drafts_and_grants_no_domain_calls(tmp_path,monkeypatch):
    import reduce_coverage as execution
    import hermes_source_units.reduce_coverage as coverage
    c=fixture(tmp_path);c.save('run.json',{'state':'draft'});c.save('global.json',{'state':'completed'})
    digest=c.save('basis.json',{'cause':'confirmed validator correction'})
    monkeypatch.setattr(coverage,'batch_outcome',lambda *_:{'coverage_complete':False})
    k=SimpleNamespace(_batch=lambda _:{'batch_id':'batch','global_reduction_id':'global','run_ids':['run']},
        _run_path=lambda _:tmp_path/'run.json',validate_run=lambda _:{'ok':True},
        _global_reduction_path=lambda *_:tmp_path/'global.json')
    a=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,knowledge=k))
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=execution.verify_validator(a,c.w,SimpleNamespace(name='checkpoint-1-validate'),{},
        {'basis':[{'ref':'basis.json','sha256':digest}]})
    assert result['validator_recovery']['new_model_calls_granted']==0
    assert result['validator_recovery']['domain_outputs_preserved']
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ContractError):execution.verify_validator(a,c.w,SimpleNamespace(name='global-reduce'),{}, {'basis':[]})


def test_validator_recovery_changes_only_validator_and_descendant_identities(vault,monkeypatch):
    from test_reduce_execution_recovery import stopped_reducer
    from ingest_kanban import _desired_graph
    a,w,_,_,_=stopped_reducer(vault,monkeypatch,'global-reduce')
    before={n.name:n.input_fingerprint for n in _desired_graph(a.workflow,w)}
    changed=copy.deepcopy(w)
    changed.setdefault('execution_recovery_history',[]).append({
        'nodes':['checkpoint-1-validate'],'input_digest':'sha256:'+'1'*64})
    after={n.name:n.input_fingerprint for n in _desired_graph(a.workflow,changed)}
    assert before['global-reduce']==after['global-reduce']
    assert before['checkpoint-1-validate']!=after['checkpoint-1-validate']
    assert all(before[n]==after[n] for n in before if n.startswith(('resource-reduce:','pass-slice:')))
