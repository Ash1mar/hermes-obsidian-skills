"""Program-only QA delivery: commit ready pages, exclude held ones, native close.

Existing controls authenticate every original review and input. This controller
grants no new semantic call and never revises a model's domain decision.
"""
import hashlib
from hermes_source_units import ContractError
from hermes_source_units.qa_policy import enabled,POLICY
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint


def recover(adapter,wid,*,resume=True):
    from automatic_pass_recovery import request
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    from finalize_delivery_flow import inventory_for,recovery_basis
    from finalize_disposition_control import inspect,apply
    from finalize_delivery_control import authorize
    from hermes_source_units.finalize_revision_state import load,work_id
    from hermes_source_units.partial_finalize import pinned,progress
    from build_execution_recovery import SCOPE,contract_hash
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-build-qa'
    with _exclusive_lock(root/'.controller.lock'):
        w=adapter.workflow.status(wid)
        if not enabled(w) or w['current_stage']!='build_finalizing' or adapter.workflow.pending_pause(w):return False
        card=next((c for c in w['kanban']['task_map'] if c['node']=='build-finalize'),None)
        fp=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint("build-finalize")[:16]}.json'
        if not card or not fp.exists():return False
        failure=_load_json(fp)
        if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='SEMANTIC_REVIEW_REQUIRED':return False
        op='auto-build-qa-'+fingerprint(card['task_id'])[:20]
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        inventory=inventory_for(adapter,adapter.workflow.status(wid));inv=pinned(adapter.workflow.vault,inventory)
        if any(i['state']=='deferred' and i['observed_status'] in ('completion_unknown','unproven_stopped_attempt','execution_held') for i in inv['items']):
            raise ContractError('RECOVERY_NOT_ALLOWED','$','execution uncertainty cannot be treated as QA')
        def saved(name,build,invoke):
            rp=root/(op+'-'+name+'.request.json')
            if not rp.exists():_write_atomic(rp,_json_bytes(build()))
            result=invoke(_load_json(rp));_write_atomic(root/(op+'-'+name+'.json'),_json_bytes(result));return result
        def control(action,**fields):
            return request(adapter,wid,action=action,inventory=inventory,
                expected_control_revision=load(adapter.workflow.vault,wid)['revision'],
                operation_id=op+'-'+action+('-'+fingerprint(fields['run_id'])[:8] if 'run_id' in fields else ''),reason='Automatic QA policy; preserve ready reviews and isolate unresolved pages without human adjudication',**fields)
        snapshot=pinned(adapter.workflow.vault,inv['snapshot'])
        for run in snapshot['backend']['runs']:
            key=work_id(run['run_id'],'*')
            if not load(adapter.workflow.vault,wid)['works'].get(key):
                saved('enable-'+fingerprint(run['run_id'])[:8],lambda run=run:control('enable-partial',run_id=run['run_id']),lambda q:apply(adapter,q))
        committed={pid for run in snapshot['backend']['runs'] for pid in progress(adapter.workflow.knowledge,run)['committed']}
        ready={i['logical_work']['page_id'] for i in inv['items'] if i['state']=='ready'}
        if ready.issubset(committed):saved('delivery',lambda:control('authorize-delivery'),lambda q:authorize(adapter,q))
        w=adapter.workflow.status(wid);basis=recovery_basis(adapter.workflow.vault,w,inventory)
        saved('recovery',lambda:request(adapter,wid,recovery_scope=SCOPE,recovery_id=op,
            target_contract_sha256=contract_hash(),reason='Automatic QA continuation; no new semantic calls, retain all outcomes',
            entries=[{'node':'build-finalize','task_id':card['task_id'],'failure_sha256':hashlib.sha256(fp.read_bytes()).hexdigest(),
                'snapshot_ref':basis['ref'],'snapshot_sha256':basis['sha256'],'retry_unknowns':[]}]),lambda q:adapter.recover_build_execution(q))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        return True
