"""Keep the trusted watcher alive until persisted final results are acknowledged.

Use ordinary supported reconciliation, never run acceptance or grant a domain
retry. The report is host-runtime evidence, outside the Vault control plane.
"""
import os
import time
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.runtime_telemetry import runtime_directory,span
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from native_stop_inventory import workflow_processes

TRANSIENT = {'KANBAN_UNAVAILABLE','REVISION_CONFLICT','LOCK_BUSY','RECONCILIATION_YIELD'}


def final_evidence(adapter,value):
    """A terminal label alone cannot certify a successful final acceptance."""
    service=adapter.workflow;wid=value['workflow_id']
    release=f"_system/knowledge-releases/{value.get('release_id')}/manifest.json"
    lint=f'_system/ledgers/ingest-workflows/{wid}/reports/acceptance-lint.json'
    finals=[r for r in value.get('artifacts',[]) if r['stage']==value['state']]
    if (value.get('current_stage') not in ('validating','completed') or not value.get('release_id')
            or not any({release,lint}.issubset(r['refs']) for r in finals)):
        raise ContractError('TERMINAL_COMPLETION_UNPROVEN','$','final release and acceptance evidence required')
    service.assert_execution_ready(value)
    if not _load_json(_vault_path(service.vault,lint)).get('ok') or not FileVaultFinalizeService(service.vault).validate(value['release_id'])['ok']:
        raise ContractError('TERMINAL_COMPLETION_UNPROVEN','$','final acceptance or release is not valid')


def settle(adapter,workflow_id,wake,interval,*,timeout=60):
    """Bounded, idempotent mechanical polls; no semantic attempt is consumed."""
    report=runtime_directory(adapter.workflow.vault,workflow_id)/'terminal-completion.json'
    deadline=time.monotonic()+timeout;attempts=0
    receipt={'contract':'terminal-native-completion/v1','workflow_id':workflow_id,
        'vault':str(adapter.workflow.vault),'domain_calls_granted':0,'domain_rerun':False}
    def persist(state,**fields):
        _write_atomic(report,_json_bytes({**receipt,'state':state,'reconciliation_attempts':attempts,**fields}))
    while True:
        value=adapter.workflow.status(workflow_id)
        if value['cancel_requested'] or value['state'] not in ('completed','partial'):
            persist('stopped');return
        try:
            with span('terminal_completion_settlement',vault=adapter.workflow.vault,workflow_id=workflow_id):
                final_evidence(adapter,value)
                attempts+=1
                # This is the existing operator reconciliation, now owned by
                # the watcher. Terminal dispatch is disabled at both entry points.
                result=adapter._sync_current(workflow_id,max_attempts=1)
                current=adapter.workflow.status(workflow_id)
                if (current['cancel_requested'] or current['state'] not in ('completed','partial')):
                    persist('stopped');return
                if result.get('background_dispatch') or result.get('program_dispatch'):
                    raise ContractError('TERMINAL_COMPLETION_UNPROVEN','$','terminal reconciliation cannot dispatch new work')
                board=current['kanban']['board_id']
                states=adapter.kanban.task_states(board)
                pending={t:s for t,s in states.items() if s in ('ready','running','triage')}
                consumers=[p for p in workflow_processes(workflow_id,board) if p['pid']!=os.getpid()]
                leased=any(s['lease']['worker_id'] or s['state']=='leased'
                    for s in adapter.workflow.knowledge._slices(current['batch_id']))
                if not pending and not consumers and not leased:
                    persist('settled',revision=current['revision'],workflow_state=current['state'],
                        actual_consumers=0,active_leases=0,runnable_or_triage_cards=0)
                    return
                persist('pending',pending_cards=pending,consumers=consumers,active_leases=bool(leased))
        except ContractError as exc:
            if exc.code not in TRANSIENT:
                persist('held',error_code=exc.code);raise
            persist('pending',error_code=exc.code)
        if time.monotonic()>=deadline:
            persist('held',error_code='TERMINAL_COMPLETION_PENDING')
            raise ContractError('TERMINAL_COMPLETION_PENDING','$','native completion has not settled; valid results retained')
        wake.wait(min(interval,max(0,deadline-time.monotonic())))
        wake.clear()
