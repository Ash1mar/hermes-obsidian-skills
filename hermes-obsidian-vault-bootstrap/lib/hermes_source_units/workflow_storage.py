"""One fresh workflow root, with ordered, protected historical segments.

The storage envelope is never a logical workflow field or an authority cache.
Every read observes the root; every referenced segment is freshly verified or
read through a live kernel-protected evidence generation. No sibling cache is
an authority. Logical schema, canonical identity and historical states survive.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import re

from .validation import ContractError,canonical_json,fingerprint,validate_record

CONTRACT='hermes-workflow-storage/v1'
MARKER='_workflow_storage'
_WRITING=ContextVar('workflow_storage_atomic_writer',default=None)


def fail(message):raise ContractError('WORKFLOW_STORAGE_INVALID','$',message)


def is_root(path):
    path=Path(path)
    return (path.suffix=='.json' and path.name.startswith('ingest-')
        and path.parent.name=='ingest-workflows' and path.parent.parent.name=='ledgers'
        and path.parent.parent.parent.name=='_system')


def compact(value):return canonical_json(value)+b'\n'


def _safe_lexical(path,vault):
    path=Path(path);vault=Path(vault)
    if not path.is_relative_to(vault):fail('workflow storage path escapes Vault')
    for parent in (path,*path.parents):
        if parent.is_symlink():fail('workflow history path contains a symlink')
        if parent==vault:break


def _references(path,root):
    if not isinstance(root,dict) or MARKER not in root:return []
    if not is_root(path):fail('storage envelope outside the authoritative workflow root')
    meta=root[MARKER]
    if (not isinstance(meta,dict) or set(meta)!={'contract','history_prefix'}
            or meta['contract']!=CONTRACT or not isinstance(meta['history_prefix'],list)
            or root.get('workflow_id')!=path.stem):fail('invalid workflow storage envelope')
    references=meta['history_prefix']
    if not references:fail('empty segmented storage envelope')
    for ordinal,pointer in enumerate(references):
        if (not isinstance(pointer,dict) or set(pointer)!={'ref','sha256'}
                or not isinstance(pointer['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',pointer['sha256'])):
            fail('invalid workflow history pointer')
        prefix=f'_system/ledgers/ingest-workflows/{path.stem}/storage/pass-history/'
        if (not isinstance(pointer['ref'],str) or not re.fullmatch(re.escape(prefix)+
                r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}/'+f'{ordinal:06d}-'+pointer['sha256']+r'\.json',pointer['ref'])):
            fail('workflow history pointer escaped its ordered namespace')
    if not isinstance(root.get('pass_revision_history',[]),list):fail('invalid inline history tail')
    return references


def segment_paths(path,root):
    vault=Path(path).parent.parent.parent.parent
    return [(vault/p['ref'],p['sha256']) for p in _references(Path(path),root)]


def hydrate(path,root,*,segments=None):
    """Private logical value; caller must copy before exposing cache objects.

    Even with a cached parsed root, every invocation calls the segment reader.
    That reader either performs a fresh hash/read or the existing current
    registry/lease/metadata guard. Final locked checks bypass sealed byte reuse.
    """
    path=Path(path)
    if not is_root(path) or not isinstance(root,dict) or MARKER not in root:return root
    from .read_accounting import read_bytes
    values=[]
    for target,expected in segment_paths(path,root):
        # Keep lexical paths: resolving a new symlink must not hide replacement.
        _safe_lexical(target,path.parent.parent.parent.parent)
        try:data=read_bytes(target,'workflow_storage.segment')
        except OSError as exc:fail('workflow history segment unavailable: '+str(exc))
        if hashlib.sha256(data).hexdigest()!=expected:fail('workflow history segment hash changed')
        key=(str(target),expected)
        if segments is not None and key in segments:value=segments[key]
        else:
            try:value=json.loads(data)
            except (ValueError,UnicodeError) as exc:fail('invalid workflow history JSON: '+str(exc))
            if not isinstance(value,dict) or value.get('state')=='running':fail('history segment is not a superseded revision')
            if segments is not None:segments[key]=value
        values.append(value)
    logical={k:v for k,v in root.items() if k!=MARKER}
    logical['pass_revision_history']=[*values,*root.get('pass_revision_history',[])]
    return logical


def decode(path,value):
    """Generic repository load: no shared mutable objects escape."""
    return hydrate(Path(path),value)


def guard_root_write(path):
    path=Path(path)
    if not is_root(path) or _WRITING.get()==str(path.absolute()) or not path.exists():return
    from .read_accounting import _raw_read_bytes
    try:value=json.loads(_raw_read_bytes(path,'workflow_storage.write_guard'))
    except (ValueError,UnicodeError) as exc:fail('cannot inspect current workflow representation: '+str(exc))
    if isinstance(value,dict) and MARKER in value:
        fail('segmented workflow writes require the logical workflow writer')


@contextmanager
def _writer(path):
    token=_WRITING.set(str(Path(path).absolute()))
    try:yield
    finally:_WRITING.reset(token)


def _fsync_directory(path):
    if os.name!='posix':return
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _atomic_root(path,data):
    from .source_units import _write_atomic
    with _writer(path):_write_atomic(path,data)
    _fsync_directory(path.parent)


def _assert_quiescent(value):
    if os.name!='posix':return
    wid=value['workflow_id'];board=value.get('kanban',{}).get('board_id')
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if not any(args):continue
            env=dict(row.split(b'=',1) for row in (proc/'environ').read_bytes().split(b'\0') if b'=' in row)
            worker=bool(env.get(b'HERMES_KANBAN_TASK')) and (env.get(b'HERMES_INGEST_WORKFLOW_ID')==wid.encode()
                or bool(board) and env.get(b'HERMES_KANBAN_BOARD')==board.encode())
            watcher=any(Path(a.decode(errors='replace')).name=='watch_ingest_workflow.py' for a in args if a) and wid.encode() in args
            if worker or watcher:fail('workflow storage migration requires drained native consumers')
        except FileNotFoundError:continue
        except PermissionError:fail('cannot establish workflow consumer quiescence')


def write(path,value):
    """All logical writes preserve the exact archived prefix and tail order."""
    from .read_accounting import _raw_read_bytes
    path=Path(path);stored=value
    if path.exists():
        root=json.loads(_raw_read_bytes(path,'workflow_storage.write_current'))
        refs=_references(path,root)
        if refs:
            # Current segment verification is not replaced by a cached verdict.
            hydrate(path,root)
            history=value.get('pass_revision_history',[])
            if len(history)<len(refs):fail('cannot remove archived workflow history')
            for item,pointer in zip(history,refs):
                if hashlib.sha256(compact(item)).hexdigest()!=pointer['sha256']:
                    fail('cannot modify archived workflow history')
            stored={**value,'pass_revision_history':history[len(refs):],MARKER:copy.deepcopy(root[MARKER])}
    _atomic_root(path,compact(stored))


def migrate(service,request):
    """Stopped, identity-bound representation operation; never resumes work.

    Publication is a single atomic root replacement. A durable prepared record
    contains only the candidate root and identities. A crash before publication
    leaves the old root authoritative; the same request can finish publication.
    A crash after publication is recognized by the candidate hash on replay.
    """
    from .source_units import _exclusive_lock,_write_atomic,_load_json
    from .read_accounting import _raw_read_bytes
    from .ingest_workflow import mutation_digest
    from .evidence_snapshot import flags,IMMUTABLE
    service._check_request(request)
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED','$','workers cannot migrate workflow storage')
    mode=request.get('mode');operation=request.get('operation_id','')
    if mode not in ('compact','segments','inline') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
        fail('invalid workflow storage operation')
    if mode=='segments' and os.name!='posix':fail('segment publication requires Linux kernel write protection')
    path=service._path(request['workflow_id'])
    _safe_lexical(service.vault/f'_system/ledgers/ingest-workflows/{request["workflow_id"]}.json',service.vault)
    record=path.with_suffix('')/'storage-operations'/f'{operation}.json'
    _safe_lexical(record,service.vault)
    with _exclusive_lock(service._lock(request['workflow_id'])):
        value=service._load(request['workflow_id'])
        if value['actor']!=request['actor']:raise ContractError('ACTOR_MISMATCH','$','workflow actor differs')
        if value['revision']!=request['expected_revision']:raise ContractError('REVISION_CONFLICT','$','workflow revision changed')
        if value['state']!='cancelled':fail('storage migration requires stopped workflow')
        _assert_quiescent(value)
        if fingerprint(value)!=request.get('logical_fingerprint'):fail('logical workflow identity differs')
        if any(row['lease']['worker_id'] or row['state']=='leased' for row in service.knowledge._slices(value['batch_id'])):
            fail('storage migration requires drained Pass leases')
        raw=_raw_read_bytes(path,'workflow_storage.migrate_root');physical=hashlib.sha256(raw).hexdigest()
        previous=_load_json(record) if record.exists() else None
        if previous:
            if previous['request']!=dict(request):fail('storage operation identity already used')
            if physical==previous['candidate_sha256']:
                return {k:v for k,v in previous.items() if k!='candidate_root'}|{'ok':True,'replayed':True,'resumed':False}
        if physical!=request.get('expected_root_sha256'):fail('workflow root bytes changed')
        root=json.loads(raw)
        if mode=='compact':candidate=root
        elif mode=='inline':candidate=value
        else:
            if MARKER in root:fail('already segmented; do not silently re-archive history')
            history=value.get('pass_revision_history',[])
            if not history or any(not isinstance(item,dict) or item.get('state')=='running' for item in history):
                fail('only superseded non-running history can be archived')
            directory=path.with_suffix('')/'storage/pass-history'/operation
            _safe_lexical(directory,service.vault)
            directory.mkdir(parents=True,exist_ok=True)
            refs=[]
            for ordinal,item in enumerate(history):
                data=compact(item);digest=hashlib.sha256(data).hexdigest()
                target=directory/f'{ordinal:06d}-{digest}.json'
                if target.is_symlink():fail('history segment is a symlink')
                if target.exists():
                    if target.read_bytes()!=data:fail('existing history segment differs')
                else:_write_atomic(target,data)
                flags(target,flags(target)|IMMUTABLE)
                if target.read_bytes()!=data:fail('history segment changed during sealing')
                refs.append({'ref':target.relative_to(service.vault).as_posix(),'sha256':digest})
            _fsync_directory(directory)
            flags(directory,flags(directory)|IMMUTABLE)
            _fsync_directory(directory.parent)
            candidate={**value,'pass_revision_history':[],MARKER:{'contract':CONTRACT,'history_prefix':refs}}
        restored=hydrate(path,candidate)
        validate_record('ingest_workflow',restored)
        if fingerprint(restored)!=fingerprint(value):fail('representation changed logical workflow identity')
        data=compact(candidate)
        prepared={'contract':CONTRACT,'request':dict(request),'source_sha256':physical,
            'candidate_sha256':hashlib.sha256(data).hexdigest(),'logical_fingerprint':fingerprint(value),
            'candidate_root':candidate,'phase':'prepared','mode':mode,'root_bytes':len(data),'revision':value['revision']}
        if previous and previous!=prepared:fail('prepared storage candidate differs')
        if not previous:
            _write_atomic(record,compact(prepared));_fsync_directory(record.parent)
        # Root remains authoritative through all preparation failures.
        if hashlib.sha256(path.read_bytes()).hexdigest()!=physical:fail('root changed before publication')
        _atomic_root(path,data)
        return {k:v for k,v in prepared.items() if k!='candidate_root'}|{'ok':True,'applied':True,'resumed':False}
