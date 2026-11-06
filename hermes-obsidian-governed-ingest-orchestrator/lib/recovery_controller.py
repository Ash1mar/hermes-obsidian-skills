"""Durable bounded supervision of mechanical recovery, independent of its child."""
import json
import subprocess
import sys
import time
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock,_load_json,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint
from hermes_source_units.recovery_control import epoch,automatic,check
from hermes_source_units.recovery_policy import effective
from hermes_source_units.runtime_telemetry import span, measured

RETRYABLE={'LOCK_BUSY','REVISION_CONFLICT','KANBAN_UNAVAILABLE','WORKER_ACTIVE','RECOVERY_DRAIN_PENDING'}


def pending(adapter,wid):
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-control'
    matches=[]
    for path in root.glob('*.json'):
        row=_load_json(path)
        if (row.get('contract')=='recovery-controller/v1' and row.get('workflow_id')==wid
                and row.get('state') in ('pending','running')
                and row.get('operator_stop_epoch')==epoch(adapter.workflow.vault,wid)):
            matches.append(row['episode'])
    if len(matches)>1:raise ContractError('EXECUTION_UNCERTAIN','$','multiple pending recovery owners; no automatic selection')
    return matches[0] if matches else None


def prepare(adapter,wid):
    """Reserve before the watcher exits. Restart uses the same episode/budget."""
    w=adapter.workflow.status(wid)
    saved=pending(adapter,wid)
    if saved and w['state'] not in ('completed','partial','failed') and not adapter.workflow.pending_pause(w):return saved
    if w['cancel_requested'] or w['state'] in ('completed','partial','failed') or adapter.workflow.pending_pause(w):
        raise ContractError('WORKFLOW_STOPPED','$','new automatic handoff requires an active unpaused workflow')
    key=fingerprint({'stage':w['current_stage'],'cards':w['kanban']['task_map'],'epoch':epoch(adapter.workflow.vault,wid)})[:24]
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-control'
    path=root/(key+'.json')
    with _exclusive_lock(root/'.handoff.lock'):
        if not path.exists():
            from orchestration import configuration
            policy=effective(w,configuration())
            _write_atomic(path,_json_bytes({'contract':'recovery-controller/v1','workflow_id':wid,'episode':key,
                'operator_stop_epoch':epoch(adapter.workflow.vault,wid),'policy':policy,
                'state':'pending','attempts':0,'history':[],'created_revision':w['revision']}))
        else:
            state=_load_json(path)
            # A prior no_action did not authorize or attempt source quarantine.
            # Re-evaluate only corrected, newly eligible source-unknown handling;
            # retain this episode, operation lineage and remaining attempt count.
            if (state['state']=='no_action' and w['current_stage']=='reducing'
                    and state['attempts']<state['policy']['controller_attempts']):
                from page_transport_recovery import select as transport_select,contract_hash as transport_hash
                from automatic_pass_recovery import enabled as recovery_enabled
                transport_code=transport_hash()
                if (recovery_enabled(w) and state.get('page_transport_contract_sha256')!=transport_code
                        and transport_select(adapter,w)):
                    state['state']='pending';state['page_transport_contract_sha256']=transport_code
                    state['history'].append({'event':'corrected_page_transport_contract','new_attempts_granted':0})
                    _write_atomic(path,_json_bytes(state))
                from source_unknown_recovery import enabled,select,contract_hash
                code=contract_hash()
                if enabled(w) and state.get('source_unknown_contract_sha256')!=code and select(adapter,w):
                    state['state']='pending';state['source_unknown_contract_sha256']=code
                    state['history'].append({'event':'corrected_source_unknown_contract','new_attempts_granted':0})
                    _write_atomic(path,_json_bytes(state))
    return key


def attempt(adapter,wid,record):
    from native_stop_inventory import workflow_processes
    from automatic_pass_recovery import recover
    with automatic(adapter.workflow.vault,wid,record['operator_stop_epoch']):
        deadline=time.monotonic()+40
        while True:
            check(adapter.workflow.vault,wid)
            w=adapter.workflow.status(wid)
            if w['state'] in ('completed','partial','failed') or adapter.workflow.pending_pause(w):return False
            if not workflow_processes(wid,w['kanban']['board_id']):break
            if time.monotonic()>=deadline:raise ContractError('RECOVERY_DRAIN_PENDING','$','native consumers have not drained')
            time.sleep(.2)
        return recover(adapter,wid)


@measured('recovery_supervisor',lambda adapter,wid,key,script:{'vault':adapter.workflow.vault,'workflow_id':wid,'episode':key})
def supervise(adapter,wid,key,script):
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-control'
    path=root/(key+'.json')
    with _exclusive_lock(root/'.supervisor.lock'):
        state=_load_json(path)
        if state['workflow_id']!=wid or state['episode']!=key:raise ValueError('controller identity mismatch')
        if state['state'] in ('applied','held','no_action'):return state
        if state['state']=='running':
            # A supervisor can die after its child committed a receipt. Wait for
            # that owner, then consume its exact result before reserving again.
            with _exclusive_lock(root/'.attempt.lock'):
                previous=root/(key+f"-attempt-{state['attempts']}.json")
                if previous.exists():
                    outcome=_load_json(previous);state['history'].append(outcome)
                    if outcome.get('ok'):
                        state['state']='applied' if outcome['applied'] else 'no_action'
                    elif outcome.get('code') not in RETRYABLE|{'CONTROLLER_INTERRUPTED'}:
                        state.update(state='held',reason=outcome.get('code','unknown'))
                    else:state['state']='pending'
                    _write_atomic(path,_json_bytes(state))
                    if state['state'] in ('applied','held','no_action'):return state
        while state['attempts']<state['policy']['controller_attempts']:
            if epoch(adapter.workflow.vault,wid)!=state['operator_stop_epoch']:
                state.update(state='held',reason='operator_cancelled');break
            state['attempts']+=1;state['state']='running'
            _write_atomic(path,_json_bytes(state))  # Reserve before spawn, never replenish after a crash.
            result_path=root/(key+f"-attempt-{state['attempts']}.json")
            command=[sys.executable,str(script),'--vault',str(adapter.workflow.vault),'--workflow-id',wid,
                     '--episode',key,'--attempt-output',str(result_path)]
            # No artificial timeout may kill a controller halfway through a commit.
            # The child uses bounded waits/locks and holds no model request.
            with span('recovery_controller_attempt',episode=key,attempt_ordinal=state['attempts'],
                      operator_stop_epoch=state['operator_stop_epoch']) as trace:
                result=subprocess.run(command,stdin=subprocess.DEVNULL,check=False)
                trace['returncode']=result.returncode
            outcome=_load_json(result_path) if result_path.exists() else {'code':'CONTROLLER_INTERRUPTED','returncode':result.returncode}
            state['history'].append(outcome)
            if outcome.get('ok'):
                state['state']='applied' if outcome['applied'] else 'no_action';break
            if outcome.get('code') not in RETRYABLE|{'CONTROLLER_INTERRUPTED'}:
                state.update(state='held',reason=outcome.get('code','unknown'));break
            state['state']='pending';_write_atomic(path,_json_bytes(state));time.sleep(1)
        else:state.update(state='held',reason='controller_attempt_budget_exhausted')
        _write_atomic(path,_json_bytes(state))
        return state
