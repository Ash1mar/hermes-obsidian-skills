"""Invocation-local pure graph reuse, never an execution permission cache."""
import hashlib,threading,time
from hermes_source_units import ContractError
from contextlib import contextmanager
from contextvars import ContextVar
from hermes_source_units.validation import fingerprint
from hermes_source_units.runtime_telemetry import span

_SCOPE=ContextVar('ingest_graph_reuse',default=None)


@contextmanager
def graph_scope():
    if _SCOPE.get() is not None:
        yield _SCOPE.get();return
    scope={'lock':threading.RLock(),'lookups':0,'hits':0,'builds':0}
    token=_SCOPE.set(scope)
    try:yield scope
    finally:_SCOPE.reset(token)


def dependencies(service,workflow):
    """Read/hash every graph file and enumerate memberships on every lookup."""
    with span('graph_dependency_scan',vault=service.vault,workflow_id=workflow.get('workflow_id')) as trace:
        with span('graph_dependency_enumeration'):
            k=service.knowledge;bid=workflow['batch_id'];batch=k._batch(bid)
            paths={k._batch_path(bid),*k._slice_root(bid).glob('*.json')}
            for tid in batch['task_ids']:
                paths.add(k._task_path(tid))
                paths.update((service.vault/f'_system/knowledge-builds/task-{tid}/passes').glob('*.json'))
        h=hashlib.sha256();read_seconds=hash_seconds=0;byte_count=0
        for path in sorted(paths):
            name=str(path).encode();started=time.monotonic()
            try:data=path.read_bytes()
            except OSError as exc:raise ContractError('SOURCE_UNAVAILABLE','$',f'graph dependency unavailable: {path}') from exc
            read_seconds+=time.monotonic()-started;started=time.monotonic()
            h.update(len(name).to_bytes(8,'big'));h.update(name);h.update(hashlib.sha256(data).digest())
            hash_seconds+=time.monotonic()-started;byte_count+=len(data)
        trace.update(files=len(paths),bytes=byte_count,read_ms=round(read_seconds*1000,3),hash_ms=round(hash_seconds*1000,3))
        return h.digest()


def derive(service,workflow,build):
    scope=_SCOPE.get();knowledge=getattr(service,'knowledge',None)
    if (scope is None or not workflow.get('batch_id') or
            not all(hasattr(knowledge,n) for n in ('_batch_path','_slice_root','_task_path'))):return build()
    before=dependencies(service,workflow)
    key=(str(service.vault),fingerprint(workflow),before)
    with span('graph_cache_lock_wait'):scope['lock'].acquire()
    try:
        with span('graph_cache_lookup') as trace:
            scope['lookups']+=1
            if scope.get('key')==key:
                trace.update(cache_hit=True);scope['hits']+=1;return list(scope['nodes'])
            old=scope.get('key')
            trace.update(cache_hit=False,invalidation_reason='cold' if old is None else
                         'workflow_or_contract' if old[:2]!=key[:2] else 'dependency_bytes_or_membership')
            scope['builds']+=1
            with span('graph_pure_build'):nodes=build()
            if dependencies(service,workflow)==before:
                scope.update(key=key,nodes=tuple(nodes))
            else:trace['invalidation_reason']='dependencies_changed_during_build'
            return nodes
    finally:scope['lock'].release()
