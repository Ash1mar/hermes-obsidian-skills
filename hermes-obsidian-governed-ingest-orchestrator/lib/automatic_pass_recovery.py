"""Trusted reconciler policy: one known-response correction, then coverage gaps.

This controller changes execution authority only. Native workers remain the
authors of every semantic result. Unknown calls and unsafe failures stay held.
"""
import hashlib,json
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic,_exclusive_lock
from hermes_source_units.validation import fingerprint

POLICY={'contract':'automatic-pass-recovery/v1','additional_attempts':1,
        'exhausted_action':'isolate_unfinished_tasks','unknown_completion_action':'hold'}


def enabled(value):
    from orchestration import configuration
    return (value['scope'].get('execution_mode')=='auto_full'
            and configuration().get('pass_failure_policy','hold')=='once_then_isolate')


def request(adapter,wid,**fields):
    value=adapter.workflow.status(wid)
    out={'workflow_id':wid,'actor':value['actor'],'expected_revision':value['revision'],**fields}
    out['input_digest']=mutation_digest(out)
    return out


def verify_isolation(adapter,value,item,failure,entry):
    from pass_local_review import exhaustion_proof
    if not enabled(value):raise ContractError('ACCESS_DENIED','$','automatic isolation policy is disabled')
    name='pass-slice:'+item['slice_id']
    card=next(c for c in value['kanban']['task_map'] if c['node']==name)
    binding=_load_json(_vault_path(adapter.workflow.vault,
        f'_system/ledgers/ingest-workflows/{value["workflow_id"]}/bindings/{fingerprint(card["idempotency_key"])}.json'))
    snapshot=adapter.kanban.task_snapshot(value['kanban']['board_id'],card['task_id'])
    proof=exhaustion_proof(adapter,value,item,binding,failure,snapshot)
    if not proof or proof!=entry.get('automatic_proof') or proof['task_ids']!=entry.get('task_ids'):
        raise ContractError('STALE_INPUT','$','automatic exclusion must match exhausted native evidence')
    return proof


def actions(adapter,value):
    """Only authenticate material-local returned failures, never transport guesses."""
    from ingest_kanban import pass_node
    from pass_local_review import verify,exhaustion_proof
    from hermes_source_units.pass_skips import skippable_failure
    out=[];wid=value['workflow_id']
    for item in adapter.workflow.knowledge._slices(value['batch_id']):
        if item['state']=='completed' or item['slice_id'] in adapter.workflow.skipped_pass_slices(value):continue
        name='pass-slice:'+item['slice_id']
        if value['dispatch_policy']['mode']=='canary' and item['slice_id'] not in value['dispatch_policy']['slice_ids']:continue
        card=next((c for c in value['kanban']['task_map'] if c['node']==name),None)
        if not card:continue
        ref=f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(name)[:16]}.json'
        path=_vault_path(adapter.workflow.vault,ref)
        if not path.exists():continue
        failure=_load_json(path)
        if failure.get('task_id')!=card['task_id']:continue
        try:
            if skippable_failure(failure)=='UNRESOLVED_NATIVE_MODEL_REQUEST':continue
        except ContractError:continue
        node=pass_node(value,item)
        if failure.get('input_fingerprint')!=node.input_fingerprint:continue
        binding=_load_json(_vault_path(adapter.workflow.vault,
            f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(card["idempotency_key"])}.json'))
        snapshot=adapter.kanban.task_snapshot(value['kanban']['board_id'],card['task_id'])
        if snapshot.get('task',{}).get('worker_pid') or any(not r.get('ended_at') for r in snapshot.get('runs',[])):continue
        try:
            proof=exhaustion_proof(adapter,value,item,binding,failure,snapshot)
            kind='isolate' if proof else 'retry'
            if not proof:proof=verify(adapter,value,item,binding,failure,snapshot)
        except ContractError:continue  # Authenticated admission is mandatory, not best effort.
        out.append({'node':name,'task_id':card['task_id'],'failure_ref':ref,
            'failure_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'action':kind,'proof':proof})
    return out


def recover(adapter,wid,*,resume=True):
    """Called automatically at a drained blocked boundary, including restart.

    Cancellation/drain, recovery, isolation and resume use supported operations.
    The intent persists before cancellation, so a crash cannot mint a new retry.
    """
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-recovery'
    with _exclusive_lock(root/'.controller.lock'):
        value=adapter.workflow.status(wid)
        if (not enabled(value) or value['current_stage']!='analyzing' or not value['batch_id']
                or adapter.workflow.pending_pause(value)):return False
        pending=root/'pending.json'
        if pending.exists():intent=_load_json(pending)
        else:
            selected=actions(adapter,value)
            if not selected:return False
            intent={'policy':POLICY,'actions':selected,'operation_id':'auto-pass-'+fingerprint(selected)[:24]}
            _write_atomic(pending,_json_bytes(intent))
        if intent['policy']!=POLICY:raise ContractError('STALE_INPUT','$','automatic recovery policy changed')
        op=intent['operation_id']
        if not value['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        for entry in intent['actions']:
            rid=op+'-'+fingerprint(entry['node'])[:8]
            saved_request=root/(rid+'.request.json')
            if entry['action']=='retry':
                if not saved_request.exists():_write_atomic(saved_request,_json_bytes(request(adapter,wid,recovery_id=rid,
                    recovery_scope='pass-local-review/v1',reason='Automatic one-correction program policy; original evidence retained',
                    entries=[{k:entry[k] for k in ('node','task_id','failure_sha256')}]))))
                adapter.recover_pass_execution(_load_json(saved_request))
            else:
                if not saved_request.exists():_write_atomic(saved_request,_json_bytes(request(adapter,wid,operation_id=rid,
                    entries=[{'slice_id':entry['node'].partition(':')[2],
                        'deferral':'automatic_retry_exhausted','task_ids':entry['proof']['task_ids'],
                        'automatic_proof':entry['proof'],'reason':'One native correction exhausted; unresolved coverage retained',
                        'evidence':[{'ref':entry['failure_ref'],'sha256':entry['failure_sha256']}]}]))))
                adapter.skip_pass_slices(_load_json(saved_request))
        complete=root/(op+'.json')
        _write_atomic(complete,_json_bytes({**intent,'state':'applied','domain_outputs_changed':False}))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        pending.unlink()
        return True
