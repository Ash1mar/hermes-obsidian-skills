"""Historical creation config stays authenticated when later plans change batch limits."""
import copy
import hashlib
import json
import pytest
from test_reduce_dependency_revision import setup_scope
from test_pass_revision import paused,vault
from test_p3_knowledge_build import workflow_request
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.source_units import _json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint
from hermes_source_units.legacy_slice_identity import verify_snapshot,creation_config
from hermes_source_units.semantic_budget import budget_policy_pin


def historical(paused,vault,monkeypatch):
    a,w,revision=setup_scope(paused,vault,monkeypatch);k=a.workflow.knowledge
    monkeypatch.setattr(a.kanban,'task_states',lambda _: {
        tid:a.kanban.task_snapshot('',tid)['task']['status'] for tid in a.kanban.by_id},raising=False)
    batch=copy.deepcopy(k._batch(w['batch_id']));slices=copy.deepcopy(k._slices(w['batch_id']))
    report_ref='_system/reports/historical-exact-plan.json'
    evidence={'workflow_id':w['workflow_id'],'boundary':'exact_plan','files':{
        k._batch_path(w['batch_id']).relative_to(vault).as_posix():hashlib.sha256(_json_bytes(batch)).hexdigest(),
        **{k._slice_path(w['batch_id'],s['slice_id']).relative_to(vault).as_posix():hashlib.sha256(_json_bytes(s)).hexdigest() for s in slices}}}
    digest='sha256:'+fingerprint(evidence)
    (vault/report_ref).write_bytes(_json_bytes({'evidence':evidence,'evidence_digest':digest}))
    snapshot={'batch':batch,'slices':slices,'workflow':{'pause_control':{
        'boundary':'exact_plan','report_ref':report_ref,'evidence_digest':digest}}}
    snapshot_ref='_system/reports/historical-plan-snapshot.json'
    (vault/snapshot_ref).write_bytes(_json_bytes(snapshot))
    w['pause_control']['history'].insert(0,{'boundary':'exact_plan','actor':'agent','continue_id':'original-plan',
        'evidence_digest':digest,'input_digest':'sha256:'+'1'*64,'report_ref':report_ref})
    a.workflow._write(w)
    # Isolated reproduction of a real amendment retaining completed legacy slices.
    changed=copy.deepcopy(batch);changed['slice_config'].update(slice_max_tasks=12,semantic_budget_policy=budget_policy_pin())
    _write_atomic(k._batch_path(w['batch_id']),_json_bytes(changed))
    revision.update(evidence_digest='sha256:'+fingerprint(w['pause_control']))
    revision['input_digest']=mutation_digest(revision)
    return a,w,revision,snapshot_ref,snapshot


def test_historical_revision_recovery_preserves_identity_and_validates_old_config(paused,vault,monkeypatch):
    a,w,revision,ref,original=historical(paused,vault,monkeypatch)
    a.revise_passes(revision);current=a.workflow.status(w['workflow_id'])
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id']);sid=current['pass_revision']['slice_ids'][0]
    card=next(c for c in current['kanban']['task_map'] if c['node']=='pass-slice:'+sid)
    with pytest.raises(ContractError,match='slice input fingerprint changed'):
        a.worker_begin({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id'],'worker_id':'old'})
    a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    failure=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},
        'STALE_INPUT','STALE_INPUT at $: slice input fingerprint changed')
    request=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        revision_id=revision['revision_id'],recovery_id='historical-config',recovery_scope='legacy-slice-config/v1',
        historical_snapshot_ref=ref,reason='Use authenticated creation config without changing identities',
        entries=[{'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256((vault/failure).read_bytes()).hexdigest()}])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    assert not a.recover_pass_execution(request,dry_run=True)['applied']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    assert a.recover_pass_execution(request)['applied']
    current=a.workflow.status(w['workflow_id'])
    assert current['cancel_requested'] and current['pause_control']==w['pause_control']
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    fresh=a.workflow.status(w['workflow_id']);new=next(c for c in fresh['kanban']['task_map'] if c['node']==card['node'])
    assert new['task_id']!=card['task_id']
    with pytest.raises(ContractError,match='ACCESS_DENIED'):
        a.worker_begin({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id'],'worker_id':'stale'})
    assert a.worker_begin({'workflow_id':w['workflow_id'],'node':new['node'],'task_id':new['task_id'],'worker_id':'fresh'})['model_input']
    item=a.workflow.knowledge._slice(w['batch_id'],sid)
    old=next(s for s in original['slices'] if s['slice_id']==sid)
    assert item['input_fingerprint']==old['input_fingerprint'] and item['task_ids']==old['task_ids']
    assert a.workflow.knowledge._batch(w['batch_id'])['slice_config']['slice_max_tasks']==12
    assert all(p.read_bytes()==data for p,data in before.items() if 'passes' in p.parts or 'readings' in p.parts)


@pytest.mark.parametrize('tamper',['config','old_slice','template','source_input','released_digest','unbound'])
def test_historical_config_never_bypasses_authentication(paused,vault,monkeypatch,tamper):
    a,w,revision,ref,snapshot=historical(paused,vault,monkeypatch);k=a.workflow.knowledge
    item=copy.deepcopy(snapshot['slices'][0]);batch=k._batch(w['batch_id'])
    inputs=[{'task_id':tid,'fingerprint':k._slice_task_input(batch,k._task(tid),remeasure=True)['fingerprint']} for tid in item['task_ids']]
    if tamper=='config':snapshot['batch']['slice_config']['slice_max_tasks']=99
    if tamper=='old_slice':snapshot['slices'][0]['input_codepoints']+=1
    if tamper=='template':item['template_hash']='0'*64
    if tamper=='source_input':inputs[0]['fingerprint']='sha256:'+'0'*64
    if tamper=='released_digest':w['pause_control']['history'][0]['evidence_digest']='sha256:'+'0'*64
    (vault/ref).write_bytes(_json_bytes(snapshot))
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match='STALE_INPUT'):
        if tamper=='unbound':creation_config(a.workflow,w,item,inputs,batch['slice_config'])
        else:verify_snapshot(a.workflow,w,item,inputs,ref)
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
