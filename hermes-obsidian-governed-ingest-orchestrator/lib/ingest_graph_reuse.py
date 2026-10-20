"""Invocation-local pure graph reuse, never an execution permission cache."""
import hashlib,threading
from hermes_source_units import ContractError
from contextlib import contextmanager
from contextvars import ContextVar
from hermes_source_units.validation import fingerprint

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
    k=service.knowledge;bid=workflow['batch_id'];batch=k._batch(bid)
    paths={k._batch_path(bid),*k._slice_root(bid).glob('*.json')}
    for tid in batch['task_ids']:
        paths.add(k._task_path(tid))
        paths.update((service.vault/f'_system/knowledge-builds/task-{tid}/passes').glob('*.json'))
    h=hashlib.sha256()
    for path in sorted(paths):
        name=str(path).encode()
        try:data=path.read_bytes()
        except OSError as exc:raise ContractError('SOURCE_UNAVAILABLE','$',f'graph dependency unavailable: {path}') from exc
        h.update(len(name).to_bytes(8,'big'));h.update(name);h.update(hashlib.sha256(data).digest())
    return h.digest()


def derive(service,workflow,build):
    scope=_SCOPE.get();knowledge=getattr(service,'knowledge',None)
    if (scope is None or not workflow.get('batch_id') or
            not all(hasattr(knowledge,n) for n in ('_batch_path','_slice_root','_task_path'))):return build()
    before=dependencies(service,workflow)
    key=(str(service.vault),fingerprint(workflow),before)
    with scope['lock']:
        scope['lookups']+=1
        if scope.get('key')==key:
            scope['hits']+=1;return list(scope['nodes'])
        scope['builds']+=1
        nodes=build()
        if dependencies(service,workflow)==before:
            scope.update(key=key,nodes=tuple(nodes))
        return nodes
