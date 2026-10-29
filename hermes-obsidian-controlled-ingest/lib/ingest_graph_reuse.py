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
from hermes_source_units.verified_snapshot import VerifiedSnapshot
from hermes_source_units.read_accounting import read_bytes
from hermes_source_units.dependency_views import FrozenProof
import hermes_source_units.validation as contracts

_SCOPE=ContextVar('ingest_graph_reuse',default=None)
_PROOF=ContextVar('ingest_graph_proof',default=None)


@contextmanager
def graph_scope():
    if _SCOPE.get() is not None:
        yield _SCOPE.get();return
    scope={'condition':threading.Condition(),'lanes':{},'lookups':0,'hits':0,'builds':0,'cohorts':0,
           'fingerprint_calls':0,'fingerprint_ns':0,'fingerprint_reuses':0,'proof_copy_ns':0,
           'fingerprint_requests':0,'fingerprint_cache_hits':0,'fingerprint_fallback_calls':0,
           'proof_internal_shares':0,'proof_external_copies':0,'proof_external_fields':0,
           'proof_envelope_builds':0,'proof_build_ns':0}
    token=_SCOPE.set(scope)
    proof_token=_PROOF.set(None)
    try:yield scope
    finally:
        with span('graph_reuse_summary',**{k:scope[k] for k in ('lookups','hits','builds','cohorts',
                'fingerprint_calls','fingerprint_ns','fingerprint_reuses','proof_copy_ns',
                'fingerprint_requests','fingerprint_cache_hits','fingerprint_fallback_calls',
                'proof_internal_shares','proof_external_copies','proof_external_fields',
                'proof_envelope_builds','proof_build_ns')}):pass
        _PROOF.reset(proof_token)
        _SCOPE.reset(token)


def workflow_fingerprint(workflow):
    """Same canonical identity; only immutable fresh-read snapshots can memoize."""
    if isinstance(workflow,VerifiedSnapshot):
        value,built,elapsed=workflow.canonical_result();fallback=False
    else:
        started=time.monotonic_ns();value=fingerprint(workflow)
        built=True;elapsed=time.monotonic_ns()-started;fallback=True
    scope=_SCOPE.get()
    if scope is not None:
        with scope['condition']:
            scope['fingerprint_requests']+=1
            scope['fingerprint_calls']+=int(built);scope['fingerprint_ns']+=elapsed
            scope['fingerprint_cache_hits']+=int(not built)
            scope['fingerprint_fallback_calls']+=int(fallback)
    return value


def dependencies(service,workflow,*,_internal=False,**fields):
    """Enumerate and hash current bytes, retaining them for the pure build."""
    from hermes_source_units.evidence_snapshot import current
    session=current()
    if session is not None:return session.graph(workflow,internal=_internal)
    with span('graph_dependency_scan',vault=service.vault,workflow_id=workflow.get('workflow_id'),
              trigger='desired_graph',**fields) as trace:
        k=service.knowledge;bid=workflow['batch_id'];started=time.monotonic_ns();batch_path=k._batch_path(bid)
        path_ns=time.monotonic_ns()-started;batch_ns=directory_ns=0
        records={};h=hashlib.sha256();read_seconds=hash_seconds=byte_count=0;names={};directory_calls=0
        def name(path):
            if path not in names:names[path]=str(path)
            return names[path]
        def read(path):
            nonlocal read_seconds,hash_seconds,byte_count
            started=time.monotonic()
            try:data=read_bytes(path,'graph_dependency_scan')
            except OSError as exc:
                raise ContractError('SOURCE_UNAVAILABLE','$',f'graph dependency unavailable: {path}') from exc
            read_seconds+=time.monotonic()-started;started=time.monotonic()
            digest=hashlib.sha256(data).digest();records[path]=(data,digest)
            hash_seconds+=time.monotonic()-started;byte_count+=len(data)
        read(batch_path)
        directories={}
        with span('graph_dependency_enumeration'):
            started=time.monotonic_ns()
            cache=getattr(service,'_verified_json_reads',None)
            if cache is None:
                with record_snapshot(records):batch=k._batch(bid)
            else:
                batch=cache.captured(batch_path,records[batch_path][0],kind='knowledge_batch',validator=contracts.validate_record,
                    project=lambda value:{'task_ids':value['task_ids']},projection_key=('scan_batch_tasks',),origin='graph.batch_tasks')
            batch_ns=time.monotonic_ns()-started
            def members(directory):
                nonlocal directory_ns,directory_calls
                started=time.monotonic_ns()
                found=tuple(directory.glob('*.json'))
                directories[name(directory)]=sorted(name(p) for p in found);directory_calls+=1
                directory_ns+=time.monotonic_ns()-started
                return found
            started=time.monotonic_ns();slice_root=k._slice_root(bid);path_ns+=time.monotonic_ns()-started
            paths={batch_path,*members(slice_root)}
            for tid in batch['task_ids']:
                started=time.monotonic_ns();task=k._task_path(tid)
                directory=service.vault/f'_system/knowledge-builds/task-{tid}/passes'
                path_ns+=time.monotonic_ns()-started;paths.add(task)
                paths.update(members(directory))
        for path in sorted(paths):
            if path not in records:read(path)
            started=time.monotonic();encoded=name(path).encode()
            h.update(len(encoded).to_bytes(8,'big'));h.update(encoded);h.update(records[path][1])
            hash_seconds+=time.monotonic()-started
        trace.update(files=len(paths),bytes=byte_count,read_ms=round(read_seconds*1000,3),
                     hash_ms=round(hash_seconds*1000,3),batch_parse_validate_ns=batch_ns,
                     directory_enumeration_ns=directory_ns,path_construct_resolve_ns=path_ns,
                     directory_enumerations=directory_calls,unique_path_strings=len(names),
                     batch_task_projection=cache is not None)
        return {'digest':h.digest(),'records':records,'files':len(paths),'bytes':byte_count,
                'directories':directories,'proof_files':{name(p):digest.hex() for p,(_,digest) in records.items()}}


def _calculate(service,workflow,build,lane,scope,cohort,size,canonical):
    meta={'cohort_id':cohort,'shared_callers':size}
    before=dependencies(service,workflow,_internal=True,scan_role='lookup',**meta)
    # Installed code cannot change under active consumers. Include actual schema
    # bytes independently of workflow revision, file sizes and timestamps too.
    schema=Path(contracts.__file__).with_name('schemas')/'contracts.json'
    from hermes_source_units.read_accounting import read_hashed
    contract=bytes.fromhex(read_hashed(schema,'graph_schema')[1])
    started=time.monotonic_ns()
    lane['proof']=(FrozenProof(before['proof_files'],before['directories'],contract.hex())
        if before.get('_sealed_internal') else {'files':before['proof_files'],
        'directories':before['directories'],'schema_sha256':contract.hex()})
    scope['proof_envelope_builds']+=1;scope['proof_build_ns']+=time.monotonic_ns()-started
    # This owned workflow snapshot is read-only throughout the pure calculation.
    # Reuse its canonical value, never dictionary identity or raw-file SHA.
    key=(canonical,contract,before['digest']);scope['fingerprint_reuses']+=1
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
        after=dependencies(service,workflow,_internal=True,scan_role='build_confirmation',**meta)
        if after['digest']!=before['digest']:
            trace['invalidation_reason']='dependencies_changed_during_build'
            raise ContractError('SOURCE_CHANGED','$','graph dependencies changed during calculation')
        lane.update(key=key,nodes=nodes)
        return nodes


def checked_dependencies(service,workflow,*,canonical=None):
    """Copy the graph's actual byte/member proof for one adjacent Build call."""
    scope=_SCOPE.get()
    captured=_PROOF.get()
    if scope is None or captured is None:return None
    current=workflow_fingerprint(workflow) if canonical is None else canonical
    if canonical is not None:scope['fingerprint_reuses']+=1
    if captured[:2]!=(str(service.vault),current):
        # Standalone worker checks may not own a graph_scope. Do not offer the
        # optimized path without an actual captured graph proof.
        return None
    started=time.monotonic_ns();value=captured[2]
    proof=value.detached() if isinstance(value,FrozenProof) else copy.deepcopy(value)
    scope['proof_copy_ns']+=time.monotonic_ns()-started;scope['proof_external_copies']+=1
    scope['proof_external_fields']+=len(proof['files'])+len(proof['directories'])
    return proof


def recheck_dependencies(service,proof):
    """Hash current bytes/members; reuse no permission, timestamp or old result."""
    from hermes_source_units.evidence_snapshot import current
    session=current()
    if session is not None and session.recheck(proof):return
    with span('build_dependency_recheck',vault=service.vault,files=len(proof['files'])) as trace:
        schema=Path(contracts.__file__).with_name('schemas')/'contracts.json'
        from hermes_source_units.read_accounting import read_hashed
        if read_hashed(schema,'dependency_recheck.schema')[1]!=proof['schema_sha256']:
            raise ContractError('SOURCE_CHANGED','$','Build schema changed after full check')
        directory_started=time.monotonic_ns()
        for name,expected in proof['directories'].items():
            if sorted(str(p) for p in Path(name).glob('*.json'))!=expected:
                raise ContractError('SOURCE_CHANGED','$','Build dependency membership changed after full check')
        trace.update(directory_enumeration_ns=time.monotonic_ns()-directory_started,directory_enumerations=len(proof['directories']))
        byte_count=read_ns=hash_ns=read_files=0
        try:
            for name,expected in proof['files'].items():
                started=time.monotonic_ns()
                try:data=read_bytes(Path(name),'build_dependency_recheck')
                except OSError as exc:
                    raise ContractError('SOURCE_UNAVAILABLE','$','Build dependency unavailable after full check') from exc
                read_ns+=time.monotonic_ns()-started;byte_count+=len(data);read_files+=1
                started=time.monotonic_ns();actual=hashlib.sha256(data).hexdigest();hash_ns+=time.monotonic_ns()-started
                if actual!=expected:
                    raise ContractError('SOURCE_CHANGED','$','Build dependency bytes changed after full check')
        finally:trace.update(bytes=byte_count,read_files=read_files,read_ns=read_ns,hash_ns=hash_ns)


def derive(service,workflow,build):
    scope=_SCOPE.get();knowledge=getattr(service,'knowledge',None)
    if (scope is None or not workflow.get('batch_id') or
            not all(hasattr(knowledge,n) for n in ('_batch_path','_slice_root','_task_path'))):return build()
    # Separate workflow/control identities never share. Scope is only this native
    # invocation, including copied thread contexts, never a cross-run disk cache.
    identity=(str(service.vault),workflow_fingerprint(workflow))
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
                nodes=_calculate(service,workflow,build,lane,scope,cohort,len(members),identity[1])
                proof=lane['proof']
                if isinstance(proof,FrozenProof):scope['proof_internal_shares']+=1
                result={'nodes':nodes,'proof':proof if isinstance(proof,FrozenProof) else copy.deepcopy(proof),
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
