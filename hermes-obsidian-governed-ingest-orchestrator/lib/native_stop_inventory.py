"""Read actual native consumers, including open workers outside the Vault map."""
from pathlib import Path
import time

from hermes_source_units import ContractError


def workflow_processes(workflow_id, board_id, *, proc_root=Path('/proc')):
    rows=[]
    for proc in proc_root.glob('[0-9]*'):
        try:
            args=[a.decode(errors='replace') for a in (proc/'cmdline').read_bytes().split(b'\0') if a]
            if not args:continue  # Exited/zombie processes cannot consume code.
            markers={}
            for value in (proc/'environ').read_bytes().split(b'\0'):
                key,_,val=value.partition(b'=')
                if key in (b'HERMES_KANBAN_TASK',b'HERMES_KANBAN_BOARD',b'HERMES_INGEST_WORKFLOW_ID',b'HERMES_PROTECTED_READER_WORKFLOW'):
                    markers[key.decode()]=val.decode(errors='replace')
            native_worker=bool(markers.get('HERMES_KANBAN_TASK')) and (
                markers.get('HERMES_KANBAN_BOARD')==board_id or
                markers.get('HERMES_INGEST_WORKFLOW_ID')==workflow_id)
            watcher=any(Path(a).name=='watch_ingest_workflow.py' for a in args) and any(
                a=='--workflow-id' and args[i+1:i+2]==[workflow_id] for i,a in enumerate(args))
            reader=markers.get('HERMES_PROTECTED_READER_WORKFLOW')==workflow_id
            if native_worker or watcher or reader:
                rows.append({'pid':int(proc.name),'task_id':markers.get('HERMES_KANBAN_TASK'),
                    'kind':'native_worker' if native_worker else 'reconciler' if watcher else 'protected_reader'})
        except FileNotFoundError:
            continue  # Process exit during the scan.
        except PermissionError as exc:
            raise ContractError('RUNTIME_UNVERIFIED','$','cannot inspect native process scope') from exc
    return rows


def verify_cancel_drain(adapter, workflow_id, *, timeout=30):
    value=adapter.workflow.status(workflow_id)
    if not value['cancel_requested'] or value['state']!='cancelled':
        raise ContractError('WORKFLOW_STOPPED','$','cancel receipt cannot attest a resumed workflow')
    board=value['kanban']['board_id']
    deadline=time.monotonic()+timeout
    while True:
        live=workflow_processes(workflow_id,board)
        if not live:break
        if time.monotonic()>=deadline:
            raise ContractError('WORKER_ACTIVE','$','cancelled workflow still has native consumers')
        time.sleep(.2)
    states=adapter.kanban.task_states(board) if board else {}
    # This board belongs to one workflow. Do not limit inspection to task_map:
    # a decomposer-created worker lacks a canonical Vault binding.
    unsafe={tid:state for tid,state in states.items() if state in ('ready','running','triage')}
    if unsafe:
        raise ContractError('NATIVE_ROUTING_CONFLICT','$','cancelled board has runnable or triage cards')
    return {'actual_consumers':0,'runnable_or_triage_cards':0,'native_board_checked':board}

def verify_pause_drain(adapter,workflow_id,*,timeout=30):
    value=adapter.workflow.status(workflow_id)
    if value['cancel_requested'] or not value.get('pause_control',{}).get('boundary'):
        raise ContractError('WORKFLOW_PAUSED','$','live latched pause required')
    from hermes_source_units.runtime_telemetry import notify_reconciler
    notify_reconciler(adapter.workflow.vault,workflow_id)
    deadline=time.monotonic()+timeout
    while workflow_processes(workflow_id,value['kanban']['board_id']):
        if time.monotonic()>=deadline:raise ContractError('WORKER_ACTIVE','$','paused consumers have not drained')
        time.sleep(.2)
    board=value['kanban']['board_id'];states=adapter.kanban.task_states(board) if board else {}
    if any(s in ('ready','running','triage') for s in states.values()):
        raise ContractError('NATIVE_ROUTING_CONFLICT','$','paused board has runnable cards')
    if any(s['state']=='leased' or s['lease']['worker_id'] for s in adapter.workflow.knowledge._slices(value['batch_id'])):
        raise ContractError('WORKER_ACTIVE','$','paused workflow has live leases')
    adapter.workflow._verify_pause_snapshot(adapter.workflow.status(workflow_id))
    return {'actual_consumers':0,'active_leases':0,'runnable_or_triage_cards':0,'boundary':value['pause_control']['boundary'],'paused':True}

def settle_workflow(adapter,workflow_id,operation_id):
    """Normal arrival preserves a valid pause. Other exits use normal cancel."""
    from native_ingest import operator_only,control_workflow
    operator_only();w=adapter.workflow.status(workflow_id)
    if w.get('pause_control',{}).get('boundary') and not w['cancel_requested']:
        return verify_pause_drain(adapter,workflow_id)
    if w['state']!='cancelled':control_workflow(adapter.workflow.vault,workflow_id,'cancel',operation_id)
    return verify_cancel_drain(adapter,workflow_id)
