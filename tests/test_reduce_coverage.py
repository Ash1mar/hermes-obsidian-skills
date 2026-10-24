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
        'omitted_candidate_refs':[{'pass_id':'pass','candidate_id':'gap'}]}
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
    monkeypatch.setattr(FileIngestWorkflowService,'status',lambda *_:c.w)
    run={'actor':'agent','coverage_scope':c.scope['ref'],'decisions':[{**c.page,'content':c.content}]}
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
    w['checkpoints']['checkpoint_1']={'state':'approved','approval_digest':'sha256:'+'1'*64,
        'approved_by':'agent','decision_ref':None,'decision_sha256':None}
    a.workflow._write(w)
    (vault/'_system/test-pause.json').write_text('{"pause":"old"}')
    batch=a.workflow.knowledge._batch(w['batch_id']);batch['run_ids']=['isolated-retired'];batch['global_reduction_id']='1'*64
    _write_atomic(a.workflow.knowledge._batch_path(batch['batch_id']),_json_bytes(batch))
    path=a.workflow.knowledge._run_path('isolated-retired');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'state':'draft','coverage_scope':scope['ref']}))
    monkeypatch.setattr(a.workflow.knowledge,'_global_reduction',lambda *_:{'coverage_scope':scope['ref']})
    # A real ended validator identity is required; fixture board uses an archived card.
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
    assert all(p.read_bytes()==raw for p,raw in protected.items())
    assert reopen.recover(a,req)['replayed']
    assert not reopen.pending_path(a.workflow,w['workflow_id']).exists()


from test_p3_knowledge_build import vault
