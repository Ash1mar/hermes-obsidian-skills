"""Audited runtime capacity independent of immutable slice input configuration."""
import hashlib
import json
import os
import re

from .validation import ContractError, fingerprint
from .source_units import _exclusive_lock, _load_json, _vault_path, _json_bytes, _write_atomic


def effective(vault, batch):
    control=batch.get('pass_capacity_control')
    if control is None:return batch['slice_config']['pass_worker_concurrency']
    path=_vault_path(vault,control['request_ref']);data=path.read_bytes();request=json.loads(data)
    if (hashlib.sha256(data).hexdigest()!=control['request_sha256']
            or request['batch_id']!=batch['batch_id'] or request['actor']!=batch['actor']
            or request['limit']!=control['limit']
            or request['input_digest']!='sha256:'+fingerprint({k:v for k,v in request.items() if k!='input_digest'})):
        raise ContractError('STALE_INPUT','$','Pass capacity control differs from its audit')
    return control['limit']


def set_capacity(adapter, workflow_id, limit, operation_id):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED','$','capacity changes require trusted operator')
    if type(limit) is not int or not 1<=limit<=10 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation_id):
        raise ContractError('INVALID_SCHEMA','$','capacity must be 1 through 10 with a stable operation ID')
    from .ingest_workflow import WORKFLOW_ROOT
    from pathlib import Path
    from contextlib import ExitStack
    service=adapter.workflow;knowledge=service.knowledge
    with ExitStack() as stack:
        stack.enter_context(_exclusive_lock(_vault_path(service.vault,f'{WORKFLOW_ROOT}/.dispatch-locks/{workflow_id}.lock')))
        stack.enter_context(_exclusive_lock(service._lock(workflow_id)))
        w=service.status(workflow_id)
        if w['state']!='cancelled' or not w['cancel_requested'] or w['current_stage']!='analyzing' or not w['batch_id']:
            raise ContractError('INVALID_TRANSITION','$','capacity change requires stopped analyzing workflow')
        from native_stop_inventory import verify_cancel_drain
        verify_cancel_drain(adapter,workflow_id,timeout=0)
        stack.enter_context(_exclusive_lock(knowledge._batch_lock_path(w['batch_id'])))
        batch=knowledge._batch(w['batch_id'])
        if not batch['cancel_requested'] or any(s['lease']['worker_id'] for s in knowledge._slices(w['batch_id'])):
            raise ContractError('WORKER_ACTIVE','$','batch must have no live leases')
        service.assert_execution_ready(w);service._verify_execution_evidence(w)
        ref=f'{WORKFLOW_ROOT}/{workflow_id}/capacity/{operation_id}.json';path=_vault_path(service.vault,ref)
        if path.exists():
            request=_load_json(path)
            if request['workflow_id']!=workflow_id or request['batch_id']!=batch['batch_id'] or request['limit']!=limit:
                raise ContractError('IDEMPOTENCY_CONFLICT','$','capacity operation differs')
            expected={'limit':limit,'request_ref':ref,'request_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            if batch.get('pass_capacity_control')==expected:return {'ok':True,'replayed':True,**expected}
            if batch['revision']!=request['batch_revision']:
                raise ContractError('STALE_INPUT','$','interrupted capacity operation has a changed batch')
        else:
            request={'contract':'pass-runtime-capacity/v1','workflow_id':workflow_id,'actor':w['actor'],
                'batch_id':batch['batch_id'],'batch_revision':batch['revision'],'workflow_revision':w['revision'],
                'operation_id':operation_id,'previous_limit':effective(service.vault,batch),'limit':limit,
                'previous_control':batch.get('pass_capacity_control')}
            request['input_digest']='sha256:'+fingerprint(request);_write_atomic(path,_json_bytes(request))
        control={'limit':limit,'request_ref':ref,'request_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        # One atomic batch pointer commit. Original slice_config and all slices/Passes stay byte-identical.
        batch={**batch,'pass_capacity_control':control}
        knowledge._store_batch(batch)
        return {'ok':True,'replayed':False,'workflow_id':workflow_id,'batch_id':batch['batch_id'],
            'previous_limit':request['previous_limit'],**control,'resumed':False}
