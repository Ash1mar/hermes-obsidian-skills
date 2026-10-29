"""One reconciliation's pure completion proofs, never execution grants."""
import copy,hashlib,json
from contextlib import contextmanager
from contextvars import ContextVar
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.read_accounting import _raw_read_bytes

_SCOPE=ContextVar('ingest_completion',default=None)


@contextmanager
def completion_scope():
    if _SCOPE.get() is not None:
        yield _SCOPE.get();return
    scope={'proofs':{},'records':{},'lookups':0,'hits':0,'builds':0};token=_SCOPE.set(scope)
    try:yield scope
    finally:_SCOPE.reset(token)


def failure_record(path):
    if not path.is_file():return None
    scope=_SCOPE.get()
    if scope is None:return _load_json(path)
    data=_raw_read_bytes(path,'completion.failure_signature');key=('failure',str(path),hashlib.sha256(data).digest())
    if key not in scope['records']:
        value=_load_json(path)
        if _raw_read_bytes(path,'completion.failure_confirmation')==data:scope['records'][key]=value
        return value
    return copy.deepcopy(scope['records'][key])


def resource_record(knowledge,batch_id,rid):
    scope=_SCOPE.get()
    if scope is None or not hasattr(knowledge,'_resource_reduction_path'):return knowledge._resource_reduction(batch_id,rid)
    path=knowledge._resource_reduction_path(batch_id,rid);data=_raw_read_bytes(path,'completion.resource_signature')
    key=('resource',str(path),hashlib.sha256(data).digest())
    if key not in scope['records']:
        value=knowledge._resource_reduction(batch_id,rid)
        if _raw_read_bytes(path,'completion.resource_confirmation')==data:scope['records'][key]=value
        return copy.deepcopy(value)
    return copy.deepcopy(scope['records'][key])


def completed(service,workflow,node,build):
    scope=_SCOPE.get();bid=workflow.get('batch_id')
    if scope is None or not bid:return build()
    k=service.knowledge
    paths=[];control={'workflow_id':workflow['workflow_id'],'batch_id':bid}
    if node.kind=='pass-slice' and hasattr(k,'_slice_path'):
        path=k._slice_path(bid,node.name.partition(':')[2]);data=_raw_read_bytes(path,'completion.slice_signature')
        # Revision-covered unfinished slices have a broader dependency closure.
        # Retain their ordinary full proof instead of inventing a wider cache.
        try:finished=json.loads(data)['state']=='completed'
        except (ValueError,KeyError,TypeError):return build()
        if not finished:return build()
        signature=hashlib.sha256(data).digest()
    elif node.kind in ('resource-reduce','global-reduce') and hasattr(k,'_batch_path'):
        batch=k._batch(bid);paths=[k._batch_path(bid)];batch_bytes=_raw_read_bytes(paths[0],'completion.batch_signature')
        if json.loads(batch_bytes)!=batch:return build()
        if node.kind=='resource-reduce':
            paths.extend(k._resource_reduction_path(bid,rid) for rid in batch.get('resource_reduction_ids',[]))
            pointers=[p for p in workflow.get('execution_recovery_history',[]) if node.name in p['nodes']]
            control['recovery']=pointers
            paths.extend(_vault_path(service.vault,p['report_ref']) for p in pointers)
        signature=tuple((str(p),hashlib.sha256(batch_bytes if i==0 else _raw_read_bytes(p,'completion.dependency_signature')).digest()) for i,p in enumerate(paths))
    else:return build()
    key=(str(service.vault),fingerprint(control),node.name,node.input_fingerprint,signature)
    scope['lookups']+=1
    if key in scope['proofs']:
        scope['hits']+=1;return scope['proofs'][key]
    scope['builds']+=1;result=build()
    # A concurrent write cannot be stored under the old proof signature.
    if node.kind=='pass-slice':stable=hashlib.sha256(_raw_read_bytes(path,'completion.slice_confirmation')).digest()==signature
    else:stable=tuple((str(p),hashlib.sha256(_raw_read_bytes(p,'completion.dependency_confirmation')).digest()) for p in paths)==signature
    if stable:scope['proofs'][key]=result
    return result
