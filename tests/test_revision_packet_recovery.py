"""A corrected fixed-revision packet can recover only its exact observed failure."""
import hashlib
import json
import pytest
from test_reduce_dependency_revision import setup_scope
from test_pass_revision import paused,vault
from test_p3_knowledge_build import workflow_request
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes


@pytest.mark.parametrize('change',['valid','foreign_reason','packet_bytes','predecessor'])
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
    ref=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},'ACCESS_DENIED',reason)
    if change=='packet_bytes':
        path=vault/context['packet_ref'];path.write_text(path.read_text()+' ')
    if change=='predecessor':
        context['candidates']['t1']='0'*64;context_ref.write_bytes(_json_bytes(context))
    request=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        revision_id=revision['revision_id'],recovery_id='fixed-packet-recovery',recovery_scope='revision-citation-packet/v1',
        reason='Retry only a corrected fixed revision transport contract',entries=[{
            'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest()}])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if change!='valid':
        with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED' if change=='foreign_reason' else 'STALE_INPUT'):
            a.recover_pass_execution(request,dry_run=True)
    else:
        result=a.recover_pass_execution(request,dry_run=True)
        assert not result['applied'] and result['entries'][0]['revision_packet_identity']['predecessors']==context['candidates']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    if change=='valid':
        assert a.recover_pass_execution(request)['applied']
        assert a.workflow.status(w['workflow_id'])['cancel_requested']
