"""Mechanical recovery is bounded and cannot invalidate domain evidence."""
import hashlib
import json
import pytest

from test_compact_execution import stopped, vault, sync
from test_p3_knowledge_build import workflow_request
from ingest_kanban import mechanical_failure_codes, pass_node
from hermes_source_units import ContractError


@pytest.mark.parametrize('report', [
    {'error_code':'SEMANTIC_REVIEW_REQUIRED'},
    {'error_code':'PROGRAM_EXECUTION_FAILED'},
    {'error_code':'PASS_EXECUTION_BLOCKED','reason':'PASS_EXECUTION_BLOCKED at $: not structured'},
    {'error_code':'PASS_EXECUTION_BLOCKED','reason':"PASS_EXECUTION_BLOCKED at $: [{'stage':'pass','code':'WORKFLOW_STOPPED'},{'stage':'pass','code':'INCOMPLETE_COVERAGE'}]"},
])
def test_recovery_rejects_nonmechanical_or_mixed_failures(report):
    with pytest.raises(ContractError, match='RECOVERY_NOT_ALLOWED'):
        mechanical_failure_codes(report)


def prepare(stopped, monkeypatch):
    adapter, value, _ = stopped
    monkeypatch.setattr('native_stop_inventory.workflow_processes', lambda *_: [])
    monkeypatch.setattr(adapter.kanban, 'task_states', lambda _: {
        t:adapter.kanban.task_snapshot('', t)['task']['status'] for t in adapter.kanban.by_id}, raising=False)
    card = next(c for c in value['kanban']['task_map'] if
        adapter.workflow.knowledge._slice(value['batch_id'], c['node'].partition(':')[2])['state']=='cancelled')
    ref = adapter.execution_failure({'workflow_id':value['workflow_id'], 'node':card['node'],
        'task_id':card['task_id']}, 'WORKFLOW_STOPPED', 'Cancelled before submission')
    request = workflow_request(workflow_id=value['workflow_id'], actor=value['actor'],
        expected_revision=value['revision'], recovery_id='recover-mechanical', reason='User-authorized cancelled attempt retry',
        entries=[{'node':card['node'], 'task_id':card['task_id'],
                  'failure_sha256':hashlib.sha256((adapter.workflow.vault/ref).read_bytes()).hexdigest()}])
    return adapter, value, card, request, ref


def test_recovery_preserves_results_and_rejects_old_grant(stopped, vault, monkeypatch):
    adapter, value, card, request, ref = prepare(stopped, monkeypatch)
    before = {p:p.read_bytes() for p in vault.rglob('*.json')}
    domain = {p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json')}
    preview = adapter.recover_pass_execution(request, dry_run=True)
    assert not preview['applied'] and {p:p.read_bytes() for p in vault.rglob('*.json')}==before
    result = adapter.recover_pass_execution(request)
    assert result['applied'] and not result['resumed']
    current = adapter.workflow.status(value['workflow_id'])
    assert current['state']=='cancelled' and current.get('pause_control')==value.get('pause_control')
    assert current['scope']['pause_after']==value['scope']['pause_after']
    assert all(p.read_bytes()==b for p,b in domain.items())
    original = json.loads((vault/ref).read_text())
    assert json.loads((vault/result['report_ref']).read_text())['entries'][0]['failure']==original
    assert adapter.recover_pass_execution(request)['replayed']
    adapter.resume(workflow_request(workflow_id=value['workflow_id'],actor=value['actor'],expected_revision=current['revision']))
    fresh = adapter.workflow.status(value['workflow_id'])
    newcard = next(c for c in fresh['kanban']['task_map'] if c['node']==card['node'])
    assert newcard['task_id']!=card['task_id'] and newcard['idempotency_key']!=card['idempotency_key']
    assert all(p.read_bytes()==b for p,b in domain.items() if 'passes' in p.parts or 'readings' in p.parts)
    with pytest.raises(ContractError,match='ACCESS_DENIED'):
        adapter.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
                              'task_id':card['task_id'],'worker_id':'stale-worker'})
    begun=adapter.worker_begin({'workflow_id':value['workflow_id'],'node':newcard['node'],
                               'task_id':newcard['task_id'],'worker_id':'fresh-worker'})
    assert begun['worker_request']['task_id']==newcard['task_id']
    # Recovery grant is one control-plane epoch, not a persistent failed-card bypass.
    adapter.execution_failure({'workflow_id':value['workflow_id'],'node':newcard['node'],
        'task_id':newcard['task_id']},'WORKFLOW_STOPPED','Repeated failure')
    item=adapter.workflow.knowledge._slice(value['batch_id'],newcard['node'].partition(':')[2])
    assert adapter._execution_blocked(value['workflow_id'],pass_node(fresh,item))


@pytest.mark.parametrize('change,code', [
    ('failure_hash','STALE_INPUT'),('actor','ACTOR_MISMATCH'),('worker','ACCESS_DENIED'),
    ('native_live','WORKER_ACTIVE'),('report_changed','STALE_INPUT'),
])
def test_recovery_guards(stopped, monkeypatch, change, code):
    adapter,value,card,request,ref=prepare(stopped,monkeypatch)
    if change=='failure_hash': request['entries'][0]['failure_sha256']='0'*64
    if change=='actor':request['actor']='foreign'
    if change=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    if change=='native_live':monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[{'pid':1}])
    if change=='report_changed':(adapter.workflow.vault/ref).write_text('{}')
    from hermes_source_units import mutation_digest
    request['input_digest']=mutation_digest(request)
    with pytest.raises(ContractError,match=code):adapter.recover_pass_execution(request)


def test_recovery_tamper_and_one_retry_limit(stopped, monkeypatch):
    adapter,value,card,request,ref=prepare(stopped,monkeypatch)
    result=adapter.recover_pass_execution(request)
    current=adapter.workflow.status(value['workflow_id'])
    repeated=workflow_request(**{k:v for k,v in request.items() if k!='input_digest'})
    repeated.update(recovery_id='another-retry',expected_revision=current['revision'])
    from hermes_source_units import mutation_digest
    repeated['input_digest']=mutation_digest(repeated)
    with pytest.raises(ContractError,match='RECOVERY_EXHAUSTED'):adapter.recover_pass_execution(repeated)
    (adapter.workflow.vault/result['report_ref']).write_text('{}')
    with pytest.raises(ContractError,match='STALE_INPUT'):adapter.workflow.assert_execution_ready(current)
