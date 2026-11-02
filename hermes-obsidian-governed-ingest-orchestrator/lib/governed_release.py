"""Explicit stopped compatibility path to a new, governed release generation."""
import os
import re

from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _vault_path, _load_json, _write_atomic, _json_bytes, _sha
from hermes_source_units.validation import record_validation_scope
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from hermes_source_units.page_governance import POLICY
from hermes_source_units.partial_finalize import pin, pinned
from native_stop_inventory import verify_cancel_drain


def prepare(adapter, request, *, dry_run=False):
    def fail(message):
        raise ContractError('GOVERNED_RELEASE_NOT_ALLOWED', '$', message)
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('trusted operator only')
    service = adapter.workflow
    service._check_request(request)
    wid = request['workflow_id']
    operation = request.get('operation_id','')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation)
            or not request.get('reason','').strip() or request.get('policy')!=POLICY
            or request.get('automatic_page_approval') is not True or request.get('stop_after')!='release_apply'):
        fail('stable operation, exact internal policy, approval and release-only boundary required')
    lock = _vault_path(service.vault,f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(lock), _exclusive_lock(service._lock(wid)), record_validation_scope():
        w = service.status(wid)
        if w['actor']!=request['actor']:
            fail('actor mismatch')
        for old in w.get('release_refresh_history',[]):
            if old['operation_id']==operation:
                if old['input_digest']!=request['input_digest']:
                    fail('operation identity conflict')
                pinned(service.vault,old['authorization'])
                return {'ok':True,'replayed':True,'resumed':False,'authorization':old['authorization']}
        w = service._mutation(request)
        if (w['state']!='cancelled' or not w['cancel_requested'] or w['current_stage']!='indexing'
                or service.pending_pause(w) or not w.get('release_id')):
            fail('requires stopped, already applied release before Provider')
        verify_cancel_drain(adapter,wid,timeout=0)
        p4 = FileVaultFinalizeService(service.vault)
        if request.get('retrieval_scope')!=p4.source.vault_manifest['vault']['id']:
            fail('explicit Vault retrieval scope required')
        expected = pin(service.vault,p4._manifest_path(w['release_id']).relative_to(service.vault).as_posix())
        if request.get('base_release')!=expected or p4.status()['current_release_id']!=w['release_id']:
            fail('current release identity changed')
        p4.validate(w['release_id'])
        manifest = _load_json(p4._manifest_path(w['release_id']))
        if not manifest.get('delivery_scopes'):
            fail('compatible exact scoped delivery required')
        from ingest_kanban import executor_for
        templates = service.pinned_templates(w)
        if any(executor_for(k,templates[k])!='program-v1' for k in (
                'vault-finalize-plan','checkpoint-2-validate','release-apply','provider-sync')):
            fail('native program release contracts required')
        # Provider administrative parking is allowed; actual execution is not.
        for card in w['kanban']['task_map']:
            if card['node']=='provider-sync':
                snap = adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],card['task_id'])
                if any(r.get('worker_pid') for r in snap['runs']):
                    fail('Provider has already run; use a separate index upgrade protocol')
        report = {'contract':'governed-release-authorization/v1','request':dict(request),
            'previous_checkpoint':w['checkpoints']['checkpoint_2'],
            'previous_release':expected,'previous_task_map':w['kanban']['task_map'],
            'preserve_domain_results':True,'new_model_calls':0,'resumed':False}
        ref = f'_system/ledgers/ingest-workflows/{wid}/release-refresh/{operation}.json'
        pointer = {'ref':ref,'sha256':_sha(_json_bytes(report))}
        result = {'ok':True,'applied':not dry_run,'resumed':False,'authorization':pointer}
        if dry_run:
            return result
        path = _vault_path(service.vault,ref)
        if path.exists() and _load_json(path)!=report:
            fail('immutable authorization conflict')
        if not path.exists():
            _write_atomic(path,_json_bytes(report))
        w.setdefault('release_refresh_history',[]).append({'operation_id':operation,
            'input_digest':request['input_digest'],'authorization':pointer,'stop_after':'release_apply'})
        w['release_id'] = None
        w['release_plan_id'] = None
        w['checkpoints']['checkpoint_2'] = {'state':'pending','approval_digest':None,'approved_by':None,
                                          'decision_ref':None,'decision_sha256':None}
        w['current_stage'] = w['resume_stage'] = 'release_planning'
        w['revision'] += 1
        service._write(w)
        return result
