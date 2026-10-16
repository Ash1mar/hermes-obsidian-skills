"""A corrected fixed-revision packet can recover only its exact observed failure."""
import hashlib
import json
import pytest
from test_reduce_dependency_revision import setup_scope
from test_pass_revision import paused,vault
from test_p3_knowledge_build import workflow_request
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes


@pytest.mark.parametrize('change',['valid','foreign_reason','packet_bytes','predecessor','cancelled_request','stopped_revision','inspection_refusal'])
def test_packet_recovery_authenticates_native_input_and_original_citation(paused,vault,monkeypatch,change):
    a,w,revision=setup_scope(paused,vault,monkeypatch)
    monkeypatch.setattr(a.kanban,'task_states',lambda _:{tid:a.kanban.task_snapshot('',tid)['task']['status'] for tid in a.kanban.by_id},raising=False)
    a.revise_passes(revision);current=a.workflow.status(w['workflow_id'])
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id']);sid=current['pass_revision']['slice_ids'][0]
    card=next(c for c in current['kanban']['task_map'] if c['node']=='pass-slice:'+sid)
    begun=a.worker_begin({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id'],
                         'worker_id':'ingest-worker-'+card['task_id']})
    workspace=vault/'observed-native';workspace.mkdir()
    (workspace/'worker-request.json').write_bytes(_json_bytes(begun['worker_request']))
    (workspace/'pass-input-descriptor.json').write_bytes(_json_bytes(begun['model_input']))
    monkeypatch.setattr('native_ingest.IngestKanbanAdapter',lambda *_a,**_k:a)
    monkeypatch.setenv('HERMES_KANBAN_TASK',card['task_id']);monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    from native_ingest import read_pass_input
    from native_pass_identity import context_path
    assert read_pass_input(vault)['ok']
    context_ref=context_path(vault,begun['worker_request'])[0]
    context=json.loads(context_ref.read_text())
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    current=a.workflow.status(w['workflow_id'])
    a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    reason='ACCESS_DENIED at $: ACCESS_DENIED at $: typed submission requires a bounded model slice'
    if change=='foreign_reason':reason='ACCESS_DENIED at $: unrelated permission failure'
    code='ACCESS_DENIED'
    if change=='cancelled_request':
        code='PROGRAM_EXECUTION_FAILED';reason='native model request did not complete; inspect its session'
        original=a.kanban.task_snapshot
        def cancelled_snapshot(*args):
            result=original(*args)
            return {**result,'runs':[{'worker_pid':123,'status':'scheduled','outcome':'scheduled',
                'summary':'Vault workflow cancelled','started_at':10,'ended_at':11}]}
        monkeypatch.setattr(a.kanban,'task_snapshot',cancelled_snapshot)
    if change=='stopped_revision':code='WORKFLOW_STOPPED';reason='Workflow cancelled before submission'
    if change=='inspection_refusal':
        code='PASS_EXECUTION_BLOCKED'
        reason="PASS_EXECUTION_BLOCKED at $: [{'stage':'pass','code':'INCOMPLETE_COVERAGE','message':'INCOMPLETE_COVERAGE at $: required reading dependency has not been inspected; retain original evidence','task':'t1'}]"
        root=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/semantic-calls";root.mkdir(exist_ok=True)
        (root/'one-call.json').write_text(json.dumps({'native_task_id':card['task_id'],'phase':'citation','semantic_attempt_limit':2,
            'task_identities':{'t1':current['pass_revision']['tasks'][0]['task_id']}}))
    ref=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},code,reason)
    if change=='packet_bytes':
        path=vault/context['packet_ref'];path.write_text(path.read_text()+' ')
    if change=='predecessor':
        context['candidates']['t1']='0'*64;context_ref.write_bytes(_json_bytes(context))
    request=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        revision_id=revision['revision_id'],recovery_id='fixed-packet-recovery',
        recovery_scope='mechanical/v1' if change in ('cancelled_request','stopped_revision') else 'revision-citation-packet/v1',
        reason='Retry only a corrected fixed revision transport contract',entries=[{
            'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest()}])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    valid=change in ('valid','cancelled_request','stopped_revision','inspection_refusal')
    if not valid:
        with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED' if change=='foreign_reason' else 'STALE_INPUT'):
            a.recover_pass_execution(request,dry_run=True)
    else:
        result=a.recover_pass_execution(request,dry_run=True)
        assert not result['applied']
        if change!='stopped_revision':assert result['entries'][0]['revision_packet_identity']['predecessors']==context['candidates']
        if change=='inspection_refusal':assert result['entries'][0]['revision_packet_identity']['remaining_semantic_attempts']==1
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    if valid:
        assert a.recover_pass_execution(request)['applied']
        assert a.workflow.status(w['workflow_id'])['cancel_requested']


@pytest.mark.parametrize('change',['missing','other_reason','completed','live','other_outcome'])
def test_transport_failure_requires_actual_ended_native_cancellation(change):
    from revision_packet_recovery import interrupted_codes
    report={'error_code':'PROGRAM_EXECUTION_FAILED','reason':'native model request did not complete; inspect its session'}
    run={'worker_pid':123,'status':'scheduled','outcome':'scheduled','summary':'Vault workflow cancelled','started_at':10,'ended_at':11}
    snapshot={'task':{},'runs':[run]}
    if change=='missing':snapshot['runs']=[]
    if change=='other_reason':report['reason']='unrelated failure'
    if change=='completed':snapshot['task']['completed_at']=11
    if change=='live':run['ended_at']=None
    if change=='other_outcome':run['summary']='Program failed'
    with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED'):interrupted_codes(report,snapshot)


@pytest.mark.parametrize('message,semantic,expected',[
 ('required reading dependency has not been inspected; retain original evidence',True,'draft_rejected'),
 ('INCOMPLETE_COVERAGE at $: required reading dependency has not been inspected; retain original evidence',True,'draft_rejected'),
 ('INCOMPLETE_COVERAGE at $: required reading dependency has not been inspected; retain original evidence',False,'recovery_required'),
 ('required reading dependency has not been inspected; retain original evidence',False,'recovery_required'),
 ('other missing evidence',True,'recovery_required')])
def test_inspection_draft_rejection_keeps_authority_failures_held(message,semantic,expected):
    from native_pass_identity import pass_failure
    assert pass_failure(ContractError('INCOMPLETE_COVERAGE','$',message),semantic=semantic)['state']==expected


def test_aggregate_inspection_receipt_retains_partial_results_and_correction(tmp_path):
    from native_ingest import compact_semantic_receipt
    message=str(ContractError('INCOMPLETE_COVERAGE','$','required reading dependency has not been inspected; retain original evidence'))
    result={'ok':False,'validated':False,'results':[{'task':'t2','created':True,'sequence':2,'pass_kind':'citation'}],
        'failures':[{'stage':'pass','code':'INCOMPLETE_COVERAGE','message':message,'item_id':'pending-task'}]}
    receipt=compact_semantic_receipt(tmp_path,{'workflow_id':'ingest-fixture'},result,['pending-task','valid-task'])
    assert receipt['state']=='draft_rejected' and receipt['next_action']=='correct_semantic_draft'
    assert receipt['recovery_owner']=='worker' and receipt['results']==result['results']
    assert receipt['failures'][0]['message']==message and receipt['failures'][0]['task']=='t1'
    assert json.loads((tmp_path/receipt['receipt_ref']).read_text())==result


@pytest.mark.parametrize('change',['valid','missing_timeline','sdk_started','audited_call'])
def test_bootstrap_recovery_cannot_repeat_a_model_attempt(tmp_path,monkeypatch,change):
    from types import SimpleNamespace
    from revision_packet_recovery import verify_bootstrap,cause_key
    assert cause_key({'error_code':'PROGRAM_EXECUTION_FAILED','reason':"'expected_revision'"})=='canonical-binding-bootstrap'
    assert cause_key({'error_code':'PROGRAM_EXECUTION_FAILED','reason':'other failure'}) is None
    workflow={'workflow_id':'isolated'};binding={'task_id':'native'}
    timeline=tmp_path/'timeline';timeline.mkdir()
    monkeypatch.setattr('hermes_source_units.runtime_telemetry.runtime_directory',lambda *_:timeline)
    if change!='missing_timeline':(timeline/'123.jsonl').write_text(json.dumps({'name':'sdk_model_request' if change=='sdk_started' else 'materials_prepare'})+'\n')
    root=tmp_path/'_system/ledgers/ingest-workflows/isolated/semantic-calls';root.mkdir(parents=True)
    if change=='audited_call':(root/'call.json').write_text(json.dumps({'native_task_id':'native'}))
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path))
    if change=='valid':verify_bootstrap(adapter,workflow,binding,{'runs':[{'worker_pid':123,'ended_at':2}]})
    else:
        with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED'):
            verify_bootstrap(adapter,workflow,binding,{'runs':[{'worker_pid':123,'ended_at':2}]})
