"""Invocation-local graph snapshots and closed scan cohorts, never permission."""
import copy
import hashlib
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
from hermes_source_units import ContractError
from hermes_source_units.record_snapshot import record_snapshot
from hermes_source_units.validation import fingerprint
from hermes_source_units.runtime_telemetry import span
import hermes_source_units.validation as contracts

_SCOPE=ContextVar('ingest_graph_reuse',default=None)
_PROOF=ContextVar('ingest_graph_proof',default=None)


@contextmanager
def graph_scope():
    if _SCOPE.get() is not None:
        yield _SCOPE.get();return
    scope={'condition':threading.Condition(),'lanes':{},'lookups':0,'hits':0,'builds':0,'cohorts':0}
    token=_SCOPE.set(scope)
    proof_token=_PROOF.set(None)
    try:yield scope
    finally:
        _PROOF.reset(proof_token)
        _SCOPE.reset(token)


def dependencies(service,workflow,**fields):
    """Enumerate and hash current bytes, retaining them for the pure build."""
    with span('graph_dependency_scan',vault=service.vault,workflow_id=workflow.get('workflow_id'),
              trigger='desired_graph',**fields) as trace:
        k=service.knowledge;bid=workflow['batch_id'];batch_path=k._batch_path(bid)
        records={};h=hashlib.sha256();read_seconds=hash_seconds=byte_count=0
        def read(path):
            nonlocal read_seconds,hash_seconds,byte_count
            started=time.monotonic()
            try:data=path.read_bytes()
            except OSError as exc:
                raise ContractError('SOURCE_UNAVAILABLE','$',f'graph dependency unavailable: {path}') from exc
            read_seconds+=time.monotonic()-started;started=time.monotonic()
            digest=hashlib.sha256(data).digest();records[path]=(data,digest)
            hash_seconds+=time.monotonic()-started;byte_count+=len(data)
        read(batch_path)
        directories={}
        with span('graph_dependency_enumeration'):
            with record_snapshot(records):batch=k._batch(bid)
            def members(directory):
                found=tuple(directory.glob('*.json'))
                directories[str(directory)]=sorted(str(p) for p in found)
                return found
            paths={batch_path,*members(k._slice_root(bid))}
            for tid in batch['task_ids']:
                paths.add(k._task_path(tid))
                paths.update(members(service.vault/f'_system/knowledge-builds/task-{tid}/passes'))
        for path in sorted(paths):
            if path not in records:read(path)
            started=time.monotonic();name=str(path).encode()
            h.update(len(name).to_bytes(8,'big'));h.update(name);h.update(records[path][1])
            hash_seconds+=time.monotonic()-started
        trace.update(files=len(paths),bytes=byte_count,read_ms=round(read_seconds*1000,3),
                     hash_ms=round(hash_seconds*1000,3))
        return {'digest':h.digest(),'records':records,'files':len(paths),'bytes':byte_count,
                'directories':directories}


def _calculate(service,workflow,build,lane,scope,cohort,size):
    meta={'cohort_id':cohort,'shared_callers':size}
    before=dependencies(service,workflow,scan_role='lookup',**meta)
    # Installed code cannot change under active consumers. Include actual schema
    # bytes independently of workflow revision, file sizes and timestamps too.
    schema=Path(contracts.__file__).with_name('schemas')/'contracts.json'
    contract=hashlib.sha256(schema.read_bytes()).digest()
    lane['proof']={'files':{str(p):digest.hex() for p,(_,digest) in before['records'].items()},
        'directories':before['directories'],'schema_sha256':contract.hex()}
    key=(fingerprint(workflow),contract,before['digest'])
    with span('graph_cache_lookup',**meta) as trace:
        scope['lookups']+=1
        old=lane.get('key')
        if old==key:
            scope['hits']+=1;trace.update(cache_hit=True)
            return lane['nodes']
        trace.update(cache_hit=False,invalidation_reason='cold' if old is None else
            'workflow_or_contract' if old[:2]!=key[:2] else 'dependency_bytes_or_membership')
        scope['builds']+=1
        with span('graph_pure_build',**meta),record_snapshot(before['records']):
            nodes=tuple(build())
        # A captured view is input, not proof that the underlying disk stood still.
        after=dependencies(service,workflow,scan_role='build_confirmation',**meta)
        if after['digest']!=before['digest']:
            trace['invalidation_reason']='dependencies_changed_during_build'
            raise ContractError('SOURCE_CHANGED','$','graph dependencies changed during calculation')
        lane.update(key=key,nodes=nodes)
        return nodes


def checked_dependencies(service,workflow):
    """Copy the graph's actual byte/member proof for one adjacent Build call."""
    scope=_SCOPE.get()
    captured=_PROOF.get()
    if scope is None or captured is None or captured[:2]!=(str(service.vault),fingerprint(workflow)):
        # Standalone worker checks may not own a graph_scope. Do not offer the
        # optimized path without an actual captured graph proof.
        return None
    return copy.deepcopy(captured[2])


def recheck_dependencies(service,proof):
    """Hash current bytes/members; reuse no permission, timestamp or old result."""
    with span('build_dependency_recheck',vault=service.vault,files=len(proof['files'])):
        schema=Path(contracts.__file__).with_name('schemas')/'contracts.json'
        if hashlib.sha256(schema.read_bytes()).hexdigest()!=proof['schema_sha256']:
            raise ContractError('SOURCE_CHANGED','$','Build schema changed after full check')
        for name,expected in proof['directories'].items():
            if sorted(str(p) for p in Path(name).glob('*.json'))!=expected:
                raise ContractError('SOURCE_CHANGED','$','Build dependency membership changed after full check')
        for name,expected in proof['files'].items():
            try:actual=hashlib.sha256(Path(name).read_bytes()).hexdigest()
            except OSError as exc:
                raise ContractError('SOURCE_UNAVAILABLE','$','Build dependency unavailable after full check') from exc
            if actual!=expected:
                raise ContractError('SOURCE_CHANGED','$','Build dependency bytes changed after full check')


def derive(service,workflow,build):
    scope=_SCOPE.get();knowledge=getattr(service,'knowledge',None)
    if (scope is None or not workflow.get('batch_id') or
            not all(hasattr(knowledge,n) for n in ('_batch_path','_slice_root','_task_path'))):return build()
    # Separate workflow/control identities never share. Scope is only this native
    # invocation, including copied thread contexts, never a cross-run disk cache.
    identity=(str(service.vault),fingerprint(workflow))
    condition=scope['condition'];ticket={};leader=False
    with span('graph_dependency_share',vault=service.vault,workflow_id=workflow.get('workflow_id')) as trace:
        with condition:
            lane=scope['lanes'].setdefault(identity,{'pending':[],'active':False})
            lane['pending'].append(ticket)
            while 'nodes' not in ticket and 'error' not in ticket:
                if not lane['active'] and lane['pending'][0] is ticket:
                    # Close BEFORE I/O. Arrivals during a scan join the NEXT one.
                    # No sleep, timer or attempt to fill the body concurrency cap.
                    members=lane['pending'];lane['pending']=[];lane['active']=True
                    scope['cohorts']+=1;cohort=scope['cohorts'];leader=True
                    break
                condition.wait()
        if leader:
            try:
                nodes=_calculate(service,workflow,build,lane,scope,cohort,len(members))
                result={'nodes':nodes,'proof':copy.deepcopy(lane['proof']),
                    'cohort_id':cohort,'shared_callers':len(members)}
            except BaseException as exc:
                result={'error':exc,'cohort_id':cohort,'shared_callers':len(members)}
            finally:
                with condition:
                    for member in members:member.update(result)
                    lane['active']=False
                    condition.notify_all()
        trace.update(cohort_id=ticket['cohort_id'],shared_callers=ticket['shared_callers'],scan_leader=leader)
        if 'error' in ticket:raise ticket['error']
        _PROOF.set((*identity,ticket['proof']))
        return list(ticket['nodes'])
