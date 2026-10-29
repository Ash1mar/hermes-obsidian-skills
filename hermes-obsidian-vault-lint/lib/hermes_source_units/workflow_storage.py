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
PROTECTED_CONTRACT='hermes-workflow-storage/v2'
PROTECTED='pass_revision_protected_files'
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
    if (not isinstance(meta,dict) or meta.get('contract') not in (CONTRACT,PROTECTED_CONTRACT)
            or set(meta)!=({'contract','history_prefix',PROTECTED} if meta.get('contract')==PROTECTED_CONTRACT else {'contract','history_prefix'})
            or not isinstance(meta['history_prefix'],list)
            or root.get('workflow_id')!=path.stem):fail('invalid workflow storage envelope')
    references=meta['history_prefix']
    if not references and meta['contract']==CONTRACT:fail('empty segmented storage envelope')
    for ordinal,pointer in enumerate(references):
        if (not isinstance(pointer,dict) or set(pointer)!={'ref','sha256'}
                or not isinstance(pointer['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',pointer['sha256'])):
            fail('invalid workflow history pointer')
        prefix=f'_system/ledgers/ingest-workflows/{path.stem}/storage/pass-history/'
        if (not isinstance(pointer['ref'],str) or not re.fullmatch(re.escape(prefix)+
                r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}/'+f'{ordinal:06d}-'+pointer['sha256']+r'\.json',pointer['ref'])):
            fail('workflow history pointer escaped its ordered namespace')
    if not isinstance(root.get('pass_revision_history',[]),list):fail('invalid inline history tail')
    if meta['contract']==PROTECTED_CONTRACT:_protected_reference(path,root)
    return references


def _revision_binding(revision):
    if (not isinstance(revision,dict) or not isinstance(revision.get('revision_id'),str)
            or not revision['revision_id'] or not isinstance(revision.get('input_digest'),str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}',revision['input_digest'])):
        fail('protected list requires a complete revision identity')
    return {k:revision[k] for k in ('revision_id','input_digest')}


def _protected_reference(path,root):
    meta=root.get(MARKER,{})
    if meta.get('contract')!=PROTECTED_CONTRACT:return None
    pointer=meta.get(PROTECTED)
    if (not isinstance(pointer,dict) or set(pointer)!={'ref','sha256','revision_id','input_digest'}
            or not isinstance(pointer['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',pointer['sha256'])):
        fail('invalid protected list pointer')
    revision=root.get('pass_revision')
    identity=_revision_binding(revision)
    if any(pointer[k]!=v for k,v in identity.items()):fail('protected list belongs to a different revision')
    if revision.get('protected_files')!=[]:fail('protected list must have only one authoritative representation')
    prefix=f'_system/ledgers/ingest-workflows/{path.stem}/storage/pass-protected/'
    if (not isinstance(pointer['ref'],str) or not re.fullmatch(re.escape(prefix)+
            r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}/'+pointer['sha256']+r'\.json',pointer['ref'])):
        fail('protected list pointer escaped its namespace')
    return pointer


def segment_paths(path,root):
    vault=Path(path).parent.parent.parent.parent
    refs=list(_references(Path(path),root))
    pointer=_protected_reference(Path(path),root) if isinstance(root,dict) else None
    if pointer is not None:refs.append(pointer)
    return [(vault/p['ref'],p['sha256']) for p in refs]


def _segment_value(path,pointer,kind,segments):
    from .read_accounting import read_bytes
    vault=path.parent.parent.parent.parent;target=vault/pointer['ref'];expected=pointer['sha256']
    _safe_lexical(target,vault)
    origin='workflow_storage.segment' if kind=='history' else 'workflow_storage.protected_files'
    try:data=read_bytes(target,origin)
    except OSError as exc:fail('workflow segment unavailable: '+str(exc))
    if hashlib.sha256(data).hexdigest()!=expected:fail('workflow segment hash changed')
    key=(str(target),expected,kind)
    if segments is not None and key in segments:return segments[key]
    try:value=json.loads(data)
    except (ValueError,UnicodeError) as exc:fail('invalid workflow segment JSON: '+str(exc))
    if kind=='history':
        if not isinstance(value,dict) or value.get('state')=='running':fail('history segment is not a superseded revision')
    elif not isinstance(value,list):fail('protected files segment is not a list')
    if segments is not None:segments[key]=value
    return value


def hydrate(path,root,*,segments=None):
    """Private logical value; caller must copy before exposing cache objects.

    Even with a cached parsed root, every invocation calls the segment reader.
    That reader either performs a fresh hash/read or the existing current
    registry/lease/metadata guard. Final locked checks bypass sealed byte reuse.
    """
    path=Path(path)
    if not is_root(path) or not isinstance(root,dict) or MARKER not in root:return root
    refs=_references(path,root)
    values=[_segment_value(path,p,'history',segments) for p in refs]
    logical={k:v for k,v in root.items() if k!=MARKER}
    if refs or 'pass_revision_history' in root:
        logical['pass_revision_history']=[*values,*root.get('pass_revision_history',[])]
    pointer=_protected_reference(path,root)
    if pointer is not None:
        logical['pass_revision']={**root['pass_revision'],
            'protected_files':_segment_value(path,pointer,'protected-files',segments)}
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
        if MARKER in root:
            # Current segment verification is not replaced by a cached verdict.
            hydrate(path,root)
            history=value.get('pass_revision_history',[])
            if len(history)<len(refs):fail('cannot remove archived workflow history')
            for item,pointer in zip(history,refs):
                if hashlib.sha256(compact(item)).hexdigest()!=pointer['sha256']:
                    fail('cannot modify archived workflow history')
            stored={**value,'pass_revision_history':history[len(refs):],MARKER:copy.deepcopy(root[MARKER])}
            if not refs and 'pass_revision_history' not in value:stored.pop('pass_revision_history')
            pointer=_protected_reference(path,root)
            if pointer is not None:
                revision=value.get('pass_revision')
                same_identity=(isinstance(revision,dict) and all(revision.get(k)==pointer[k] for k in ('revision_id','input_digest')))
                if (same_identity and hashlib.sha256(compact(revision.get('protected_files'))).hexdigest()==pointer['sha256']):
                    stored['pass_revision']={**revision,'protected_files':[]}
                else:
                    # Publish changed content/new identity INLINE in the same
                    # atomic root. Old immutable objects are never edited and
                    # cannot authorize the next revision. Re-sealing is explicit.
                    stored[MARKER]={'contract':CONTRACT,'history_prefix':stored[MARKER]['history_prefix']}
                    if not refs:stored.pop(MARKER)
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
    if mode not in ('compact','segments','inline','protected-files') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
        fail('invalid workflow storage operation')
    if mode in ('segments','protected-files') and os.name!='posix':fail('segment publication requires Linux kernel write protection')
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
        elif mode=='protected-files':
            if _protected_reference(path,root) is not None:fail('current protected list is already segmented')
            identity=_revision_binding(value.get('pass_revision'))
            if value['pass_revision'].get('state')!='accepted':fail('initial protected list sealing requires accepted current revision')
            items=value['pass_revision'].get('protected_files')
            if not isinstance(items,list):fail('current protected files is not a list')
            directory=path.with_suffix('')/'storage/pass-protected'/operation
            _safe_lexical(directory,service.vault);directory.mkdir(parents=True,exist_ok=True)
            data=compact(items);digest=hashlib.sha256(data).hexdigest();target=directory/f'{digest}.json'
            _safe_lexical(target,service.vault)
            if target.exists():
                if target.read_bytes()!=data:fail('existing protected list differs')
            else:_write_atomic(target,data)
            flags(target,flags(target)|IMMUTABLE)
            if target.read_bytes()!=data:fail('protected list changed during sealing')
            _fsync_directory(directory);flags(directory,flags(directory)|IMMUTABLE);_fsync_directory(directory.parent)
            pointer={'ref':target.relative_to(service.vault).as_posix(),'sha256':digest,**identity}
            candidate={**root,'pass_revision':{**root['pass_revision'],'protected_files':[]},
                MARKER:{'contract':PROTECTED_CONTRACT,'history_prefix':copy.deepcopy(_references(path,root)),PROTECTED:pointer}}
        else:
            if _references(path,root):fail('already segmented; do not silently re-archive history')
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
            candidate={**root,'pass_revision_history':[],MARKER:{**root.get(MARKER,{'contract':CONTRACT}),'history_prefix':refs}}
        restored=hydrate(path,candidate)
        validate_record('ingest_workflow',restored)
        if fingerprint(restored)!=fingerprint(value):fail('representation changed logical workflow identity')
        data=compact(candidate)
        prepared={'contract':candidate.get(MARKER,{}).get('contract',CONTRACT),'request':dict(request),'source_sha256':physical,
            'candidate_sha256':hashlib.sha256(data).hexdigest(),'logical_fingerprint':fingerprint(value),
            'candidate_root':candidate,'phase':'prepared','mode':mode,'root_bytes':len(data),'revision':value['revision']}
        if previous and previous!=prepared:fail('prepared storage candidate differs')
        if not previous:
            _write_atomic(record,compact(prepared));_fsync_directory(record.parent)
        # Root remains authoritative through all preparation failures.
        if hashlib.sha256(path.read_bytes()).hexdigest()!=physical:fail('root changed before publication')
        _atomic_root(path,data)
        return {k:v for k,v in prepared.items() if k!='candidate_root'}|{'ok':True,'applied':True,'resumed':False}
