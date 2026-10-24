"""Read-only inspection through the existing Reduce recovery entry point."""
import hashlib
from pathlib import Path

from hermes_source_units.citation_revision_impact import preview, fail
from hermes_source_units.source_units import _exclusive_lock, _vault_path


def contract_hash():
    import hermes_source_units.citation_revision_impact as impact
    return hashlib.sha256(Path(__file__).read_bytes() + Path(impact.__file__).read_bytes()).hexdigest()


def inspect(adapter, request, *, dry_run):
    if not dry_run:
        fail('impact inspection is read-only; it cannot grant or execute a consumed revision')
    if request.get('target_contract_sha256') != contract_hash():
        fail('citation impact contract changed')
    service = adapter.workflow
    wid = request['workflow_id']
    dispatch_lock = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(dispatch_lock), _exclusive_lock(service._lock(wid)):
        value = service._mutation(request)
        if request['actor'] != value['actor']:
            fail('inspection actor differs')
        if not service.pending_pause(value) and not (value['state'] == 'cancelled' and value['cancel_requested']):
            fail('inspect only a held or cancelled workflow')
        from native_stop_inventory import workflow_processes
        if workflow_processes(wid, value['kanban']['board_id']) or any(
                s in ('running', 'ready', 'triage', 'todo')
                for s in adapter.kanban.task_states(value['kanban']['board_id']).values()):
            fail('native executors must be drained before inspection')
        service.assert_execution_ready(value)
        service._verify_execution_evidence(value)
        with _exclusive_lock(service.knowledge._batch_lock_path(value['batch_id'])):
            if any(s['state'] == 'leased' or s['lease']['worker_id']
                   for s in service.knowledge._slices(value['batch_id'])):
                fail('live slice leases block citation inspection')
            report = preview(service, value, request.get('selections', []))
        return {'ok': True, 'applied': False, 'resumed': False,
                'new_model_calls_granted': 0, 'impact': report}
