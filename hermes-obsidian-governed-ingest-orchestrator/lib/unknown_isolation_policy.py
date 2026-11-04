"""Explicit workflow-wide adoption without rewriting the immutable startup scope."""
import os
import re
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock
from hermes_source_units.validation import fingerprint
from hermes_source_units.recovery_policy import effective


def adopt(adapter, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED', '$', 'policy adoption requires a trusted operator')
    service=adapter.workflow;service._check_request(request);wid=request['workflow_id']
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',request.get('operation_id',''))
            or request.get('unknown_completion')!='isolate_after_drain' or not request.get('reason','').strip()):
        raise ContractError('INVALID_SCHEMA','$','explicit bounded isolation adoption required')
    from native_stop_inventory import verify_cancel_drain
    from orchestration import configuration
    lock=service.vault/f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock'
    with _exclusive_lock(lock), _exclusive_lock(service._lock(wid)):
        w=service.status(wid)
        if request['actor']!=w['actor']:raise ContractError('ACTOR_MISMATCH','$','policy actor differs')
        prior=next((r for r in w.get('unknown_isolation_adoptions',[]) if r['operation_id']==request['operation_id']),None)
        if prior:
            if prior!=dict(request):raise ContractError('IDEMPOTENCY_CONFLICT','$','policy operation changed')
            effective(w,configuration())
            return {'ok':True,'applied':False,'replayed':True,'revision':w['revision']}
        w=service._mutation(request)
        if (not w['scope'].get('recovery_policy') or service.pending_pause(w)
                or w['state']!='cancelled' or not w['cancel_requested']
                or (service.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-recovery/pending.json').exists()):
            raise ContractError('INVALID_TRANSITION','$','adoption requires a pinned workflow without a durable pause')
        verify_cancel_drain(adapter,wid,timeout=0)
        policy=effective(w,configuration())
        if request.get('previous_policy_sha256')!=fingerprint(policy):
            raise ContractError('STALE_INPUT','$','effective recovery policy changed')
        if policy['unknown_completion']!='hold':
            raise ContractError('INVALID_TRANSITION','$','unknown isolation is already enabled')
        result={'ok':True,'applied':not dry_run,'resumed':False,'scope_unchanged':True,'new_calls_granted':0}
        if dry_run:return {**result,'revision':w['revision']}
        w.setdefault('unknown_isolation_adoptions',[]).append(dict(request));w['revision']+=1;service._write(w)
        return {**result,'revision':w['revision']}
