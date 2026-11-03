"""Automatic adoption of ended source-packet holds into the finite journal."""
import hashlib,json
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock


def recover(adapter,wid,*,resume=True):
    from qa_program import recover_global
    if recover_global(adapter,wid,resume=resume):return True
    from automatic_pass_recovery import enabled,request,POLICY
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    from source_hold_recovery import SCOPE,contract_hash
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-source-recovery'
    with _exclusive_lock(root/'.controller.lock'):
        w=adapter.workflow.status(wid)
        if not enabled(w) or w['current_stage']!='reducing' or adapter.workflow.pending_pause(w):return False
        pending=root/'pending.json'
        if pending.exists():intent=_load_json(pending)
        else:
            selected=[]
            for card in w['kanban']['task_map']:
                if not card['node'].startswith('resource-reduce:'):continue
                fp=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(card["node"])[:16]}.json'
                if not fp.exists():continue
                failure=_load_json(fp)
                if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='SEMANTIC_REVIEW_REQUIRED':continue
                prefix='Source remains held; valid packets preserved and later packets attempted; report: '
                reason=failure.get('reason','').removeprefix('SEMANTIC_REVIEW_REQUIRED at $: ')
                if not reason.startswith(prefix):continue
                report=_load_json(_vault_path(adapter.workflow.vault,reason[len(prefix):].split('\n')[0]))
                if report.get('contract')!='source-packets-deferred/v1' or not report['held_packets']:continue
                old=_vault_path(adapter.workflow.vault,report['input_ref']);audit_ref=report['held_packets'][0]['audit_ref']
                selected.append({'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256(fp.read_bytes()).hexdigest(),
                    'snapshot_ref':report['input_ref'],'snapshot_sha256':hashlib.sha256(old.read_bytes()).hexdigest(),
                    'native_audit_ref':audit_ref,'native_audit_sha256':hashlib.sha256(_vault_path(adapter.workflow.vault,audit_ref).read_bytes()).hexdigest()})
            if not selected:return False
            intent={'policy':POLICY,'entries':selected,'operation_id':'auto-source-'+fingerprint(selected)[:24]}
            _write_atomic(pending,_json_bytes(intent))
        if intent['policy']!=POLICY:raise ContractError('STALE_INPUT','$','automatic source policy changed')
        op=intent['operation_id']
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        rp=root/(op+'.request.json')
        if not rp.exists():_write_atomic(rp,_json_bytes(request(adapter,wid,recovery_id=op,
            recovery_scope=SCOPE,target_contract_sha256=contract_hash(),
            reason='Program finite correction and unsupported-candidate isolation policy; original native evidence retained',entries=intent['entries'])))
        result=adapter.recover_reduce_execution(_load_json(rp))
        _write_atomic(root/(op+'.json'),_json_bytes({**intent,'state':'applied','recovery_result':result,'domain_outputs_changed':False}))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        pending.unlink()
        return True
