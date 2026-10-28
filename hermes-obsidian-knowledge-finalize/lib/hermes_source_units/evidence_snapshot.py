"""Revocable Linux evidence reads backed by kernel write protection.

Ordinary mutable files never enter this cache. Only a live privileged protector
can publish an active generation after sealing and rehashing its whole manifest.
Consumers drop *all* Linux capabilities and no-new-privs before using a generation.
The registry and protected inodes have FS_IMMUTABLE_FL. Privileged maintenance
outside this protocol must stop consumers first; it is not an untrusted writer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import array
import copy
import hashlib
import json
import os
import signal
import socket
import socketserver
import stat
import threading
import time
import uuid

from .validation import ContractError, fingerprint

CONTRACT='linux-sealed-evidence/v1'
ROOT='_system/runtime/evidence-snapshots'
IMMUTABLE=0x10
_SESSION=ContextVar('sealed_evidence_session',default=None)
_BYPASS=ContextVar('sealed_evidence_full_reads',default=False)


def fail(message):raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$',message)


def assert_sealable_slices(workflow,slices):
    if (workflow['state']!='cancelled' or workflow['current_stage']!='build_finalizing'
            or workflow['checkpoints']['checkpoint_1']['state']!='approved'):
        fail('sealing needs the stopped Build stage and accepted upstream checkpoint')
    for item in slices:
        # Cancelled amendment descriptors remain graph dependencies; freezing
        # their bytes does not treat them as completed semantic work.
        if item['state'] not in ('completed','cancelled') or item['lease']['worker_id']:
            fail('cannot seal nonterminal or leased Pass work')


def flags(path,value=None):
    import fcntl
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        bits=array.array('L',[0])
        fcntl.ioctl(descriptor,0x80086601,bits,True)
        if value is not None:
            bits[0]=value;fcntl.ioctl(descriptor,0x40086602,bits,True)
        return bits[0]
    finally:os.close(descriptor)


def _identity(path,*,content=False):
    value=path.lstat()
    if stat.S_ISLNK(value.st_mode):fail('evidence path contains a symbolic link: '+str(path))
    fields=[value.st_dev,value.st_ino,stat.S_IFMT(value.st_mode)]
    if content:fields.extend((value.st_size,value.st_ctime_ns))
    return fields


def _start(pid):
    try:return Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):return None


def restrict_consumer():
    """Irreversible for this process, including future threads/exec children."""
    status=Path('/proc/self/status').read_text().splitlines()
    capability_lines=[line for line in status if line.split(':')[0] in ('CapEff','CapPrm','CapBnd','CapAmb')]
    if capability_lines and all(int(line.split(':')[1].strip(),16)==0 for line in capability_lines) and 'NoNewPrivs:\t1' in status:return
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True)
    # Keep CAP_SETPCAP effective until every bounding bit has been removed.
    for capability in range(64):
        if libc.prctl(24,capability,0,0,0) and ctypes.get_errno()!=22:
            fail('cannot remove Linux capability bounding set')
    class Header(ctypes.Structure):_fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
    class Data(ctypes.Structure):_fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    header=Header(0x20080522,0);data=(Data*2)()
    if libc.capset(ctypes.byref(header),ctypes.byref(data)) or libc.prctl(38,1,0,0,0):
        fail('cannot constrain evidence consumer privileges')
    status=Path('/proc/self/status').read_text().splitlines()
    for line in status:
        if line.split(':')[0] in ('CapEff','CapPrm','CapBnd','CapAmb') and int(line.split(':')[1].strip(),16):
            fail('evidence consumer retains Linux capabilities')
    if 'NoNewPrivs:\t1' not in status:fail('evidence consumer can regain privileges')


def binding(workflow):
    # Current leases, cancellation, pauses, quotas and task bindings remain the
    # original fresh workflow checks. This is only the static evidence binding.
    names=('workflow_id','batch_id','scope','profile','pinned_inputs','template_pins',
           'pass_revision','execution_recovery_history')
    from .verified_snapshot import VerifiedSnapshot,thaw
    if isinstance(workflow,VerifiedSnapshot):
        return workflow.select(('sealed_evidence_binding',CONTRACT),
            lambda value:fingerprint({key:thaw(value.get(key)) for key in names}))
    return fingerprint({key:workflow.get(key) for key in names})


def _socket(vault,wid):
    return '/tmp/hermes-evidence-'+fingerprint([str(vault),wid])[:24]+'.sock'


def _dump(path,value):
    """Protector-only durable registry replace; never a workflow mutation."""
    data=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    temporary=path.with_name('.'+path.name+'.new')
    with temporary.open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


@contextmanager
def _lease(directory,exclusive=False):
    import fcntl
    with (directory/'lease.lock').open('rb') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:yield
        finally:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


class Protector:
    """Privileged, separately launched protector; RPC can revoke, never write data."""
    def __init__(self,service,workflow):
        self.service=service;self.vault=service.vault;self.workflow=workflow
        self.directory=self.vault/ROOT/workflow['workflow_id']
        self.state={'contract':CONTRACT,'workflow_id':workflow['workflow_id'],
            'generation':uuid.uuid4().hex,'state':'preparing','pid':os.getpid(),
            'process_start':_start(os.getpid()),'socket':_socket(self.vault,workflow['workflow_id'])}
        self.original={};self.manifest=None;self.mutex=threading.RLock()

    def prepare(self,graph):
        started=time.monotonic();verification_bytes=0
        if self.workflow['current_stage']!='build_finalizing':fail('snapshot is limited to Build Finalize')
        if self.directory.exists():
            old=json.loads((self.directory/'state.json').read_text())
            if old['state']!='revoked':fail('previous protector must be revoked and released first')
        self.directory.mkdir(parents=True,exist_ok=True)
        (self.directory/'lease.lock').touch(exist_ok=True)
        _dump(self.directory/'state.json',self.state)
        batch=str(self.service.knowledge._batch_path(self.workflow['batch_id']))
        files={str(path):digest.hex() for path,(_,digest) in graph['records'].items() if str(path)!=batch}
        reports={str(self.vault/p['report_ref']):p['report_sha256'] for p in self.workflow.get('execution_recovery_history',[])}
        for path,digest in reports.items():
            if path in files and files[path]!=digest:fail('conflicting snapshot hashes')
            files[path]=digest
        # A directory seal protects enumeration, not just existing file bytes.
        directories=graph['directories']
        absent=[name for name in directories if not os.path.lexists(name)]
        if any(directories[name] for name in absent):fail('missing directory has expected members')
        absent_parents={}
        for name in absent:
            parent=Path(name).parent
            while not os.path.lexists(parent):parent=parent.parent
            if not parent.is_relative_to(self.vault):fail('absent dependency has no Vault ancestor')
            absent_parents[name]=str(parent)
        # Empty absent Pass directories are common for retired tasks. Seal the
        # existing parent so mkdir/symlink cannot create members behind a cache.
        # Do not create a Vault directory merely to make a snapshot possible.
        sealed=[Path(p) for p in sorted({*files,*(name for name in directories if name not in absent),
                                       *absent_parents.values()})]
        for path in sealed:
            if path.is_symlink() or not path.exists():fail('unsealable dependency: '+str(path))
            self.original[str(path)]=flags(path)
        ancestors=set()
        for path in sealed:
            for parent in (path,*path.parents):
                if not parent.is_relative_to(self.vault):break
                ancestors.add(parent)
        self.manifest={'contract':CONTRACT,'generation':self.state['generation'],
            'binding':binding(self.workflow),'files':files,'directories':directories,
            'absent_directories':absent,
            'absent_directory_parents':absent_parents,
            'task_ids':json.loads(graph['records'][Path(batch)][0])['task_ids'],
            'reports':reports,'batch':batch,'original_flags':self.original,
            'path_identity':{},'content_identity':{},
            'reader_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        # Persist recovery metadata BEFORE the first seal; a crashed preparing
        # protector is never trusted, and release can restore only recorded bits.
        _dump(self.directory/'manifest.json',self.manifest)
        try:
            for path in sealed:flags(path,self.original[str(path)]|IMMUTABLE)
            for name,expected in files.items():
                data=Path(name).read_bytes()
                verification_bytes+=len(data)
                if hashlib.sha256(data).hexdigest()!=expected:fail('dependency changed while sealing: '+name)
            for name,expected in directories.items():
                if sorted(str(p) for p in Path(name).glob('*.json'))!=expected:fail('membership changed while sealing')
            self.manifest['path_identity']={str(p):_identity(p) for p in ancestors}
            self.manifest['content_identity']={str(p):_identity(p,content=True) for p in sealed}
            _dump(self.directory/'manifest.json',self.manifest)
            self.state['manifest_sha256']=hashlib.sha256((self.directory/'manifest.json').read_bytes()).hexdigest()
            self.state['sealed_files']=len(files);self.state['sealed_directories']=len(directories)
            self.state['preparation_graph_bytes']=graph['bytes']
            self.state['preparation_verification_bytes']=verification_bytes
            self.state['preparation_seconds']=time.monotonic()-started
            from .runtime_telemetry import span
            with span('evidence_snapshot_prepared',vault=self.vault,workflow_id=self.workflow['workflow_id'],
                      generation=self.state['generation'],files=len(files),directories=len(directories),
                      graph_bytes=graph['bytes'],verification_bytes=verification_bytes,
                      preparation_seconds=self.state['preparation_seconds']):pass
        except BaseException:
            self.revoke('preparation failed');raise

    def activate(self):
        with _lease(self.directory,True):
            self.state['state']='active';_dump(self.directory/'state.json',self.state)
            for name in ('state.json','manifest.json','lease.lock'):flags(self.directory/name,flags(self.directory/name)|IMMUTABLE)
            flags(self.directory,flags(self.directory)|IMMUTABLE)

    def revoke(self,reason):
        # No lease spans a model call. An in-progress short pure read or atomic
        # domain commit finishes before this revocation's linearization point.
        # Acknowledgement is issued only after durable revoke and release.
        with self.mutex,_lease(self.directory,True):
            for path in (self.directory,self.directory/'state.json'):
                flags(path,flags(path)&~IMMUTABLE)
            self.state.update(state='revoked',reason=reason,revoked_at=time.time())
            _dump(self.directory/'state.json',self.state)
            for name,previous in reversed(list(self.original.items())):
                flags(Path(name),previous)
            for name in ('manifest.json','lease.lock'):
                path=self.directory/name;flags(path,flags(path)&~IMMUTABLE)

    def serve(self):
        protector=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request=json.loads(self.rfile.readline(16384))
                if request.get('operation')=='revoke':
                    protector.revoke(str(request.get('reason','controlled maintenance'))[:512])
                    result={'state':'revoked','generation':protector.state['generation']}
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                elif request.get('operation')=='write':
                    path=str(Path(request['path']).absolute())
                    covered=path in protector.original or any(str(p) in protector.original for p in Path(path).parents)
                    if covered:
                        # A writer might hold a domain lease: reply and let it
                        # fail before the background revoke waits for that lease.
                        threading.Thread(target=protector.revoke,args=('controlled write requested: '+path,),daemon=True).start()
                    result={'covered':covered}
                else:result={'state':protector.state['state'],'generation':protector.state['generation']}
                self.wfile.write(json.dumps(result).encode()+b'\n')
        class Server(socketserver.ThreadingUnixStreamServer):daemon_threads=True
        path=self.state['socket']
        if os.path.exists(path):os.unlink(path)
        with Server(path,Handler) as server:
            os.chmod(path,0o600);self.activate()
            def stopped(*_):raise KeyboardInterrupt
            prior=signal.signal(signal.SIGTERM,stopped)
            try:server.serve_forever(poll_interval=.2)
            finally:
                signal.signal(signal.SIGTERM,prior)
                self.revoke('protector shutdown')
                os.unlink(path)


def rpc(path,request):
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(30);stream.connect(path)
        stream.sendall(json.dumps(request).encode()+b'\n')
        return json.loads(stream.makefile('rb').readline(16384))


class EvidenceSession:
    def __init__(self,service,directory,*,restrict=True):
        self.service=service;self.directory=directory;self.data={};self.lock=threading.RLock()
        self.stats={'hits':0,'avoided_bytes':0,'first_reads':0,'first_bytes':0,'guards':0,'metadata_checks':0}
        self.sources={}
        if restrict:restrict_consumer()
        with _lease(directory):
            state=self._state()
            data=(directory/'manifest.json').read_bytes()
            if hashlib.sha256(data).hexdigest()!=state['manifest_sha256']:fail('snapshot manifest changed')
            self.manifest=json.loads(data);self.generation=state['generation']
            if self.manifest['reader_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():fail('snapshot reader changed')
            self._metadata(self.manifest['content_identity'])

    def _state(self):
        from .read_accounting import _raw_read_bytes
        try:
            raw=_raw_read_bytes(self.directory/'state.json','evidence_snapshot.registry')
            state=json.loads(raw)
            if (state['contract']!=CONTRACT or state['state']!='active'
                    or _start(state['pid'])!=state['process_start']
                    or not flags(self.directory/'state.json')&IMMUTABLE
                    or not flags(self.directory/'manifest.json')&IMMUTABLE
                    or not flags(self.directory)&IMMUTABLE):fail('snapshot is not a live sealed generation')
            if getattr(self,'generation',state['generation'])!=state['generation']:fail('snapshot generation changed')
            self.stats['guards']+=1
            return state
        except (OSError,ValueError,KeyError,TypeError) as exc:fail('snapshot registry unavailable: '+str(exc))

    def _metadata(self,paths):
        try:self._check_metadata(paths)
        except OSError as exc:fail('sealed evidence path unavailable: '+str(exc))

    def _check_metadata(self,paths):
        all_paths=set()
        for name in paths:
            target=Path(name)
            if name in self.manifest.get('absent_directories',[]):
                if os.path.lexists(target):fail('previously absent dependency directory appeared')
                target=Path(self.manifest['absent_directory_parents'][name])
            for path in (target,*target.parents):
                if str(path) not in self.manifest['path_identity']:break
                all_paths.add(str(path))
        for name in all_paths:
            self.stats['metadata_checks']+=1
            if _identity(Path(name))!=self.manifest['path_identity'][name]:fail('evidence path was replaced')
            if name in self.manifest['content_identity'] and _identity(Path(name),content=True)!=self.manifest['content_identity'][name]:fail('sealed evidence metadata changed')

    @contextmanager
    def guard(self,paths=()):
        with _lease(self.directory):
            self._state();self._metadata(paths)
            yield

    def _read(self,path,origin):
        from .read_accounting import _raw_read_bytes,_LEDGER
        name=str(path);row=self.sources.setdefault(origin,{'hits':0,'avoided_bytes':0,'first_reads':0,'first_bytes':0})
        with self.lock:
            if name not in self.data:
                data=_raw_read_bytes(path,origin)
                if hashlib.sha256(data).hexdigest()!=self.manifest['files'][name]:fail('sealed file hash mismatch')
                self.data[name]=data;self.stats['first_reads']+=1;self.stats['first_bytes']+=len(data)
                row['first_reads']+=1;row['first_bytes']+=len(data)
            else:
                self.stats['hits']+=1;self.stats['avoided_bytes']+=len(self.data[name])
                row['hits']+=1;row['avoided_bytes']+=len(self.data[name])
                ledger=_LEDGER.get()
                if ledger is not None:ledger.note(path,0,'evidence_snapshot:'+origin,captured=True)
            return self.data[name]

    def read(self,path,origin):
        name=str(path)
        if name not in self.manifest['files']:return None
        with self.guard((name,)):return self._read(path,origin)

    def graph(self,workflow):
        if binding(workflow)!=self.manifest['binding']:fail('snapshot workflow evidence binding changed')
        from .read_accounting import _raw_read_bytes
        from .runtime_telemetry import span
        with span('snapshot_dependency_reuse',vault=self.service.vault,workflow_id=workflow['workflow_id']) as trace:
            paths=[p for p in self.manifest['files'] if p not in self.manifest['reports']]
            with self.guard([*paths,*self.manifest['directories']]):
                records={Path(p):(self._read(Path(p),'graph_dependency_scan'),bytes.fromhex(self.manifest['files'][p])) for p in paths}
                batch=Path(self.manifest['batch']);data=_raw_read_bytes(batch,'graph_dependency_scan.batch')
                records[batch]=(data,hashlib.sha256(data).digest())
                # Batch stays mutable and is freshly interpreted. A changed
                # dependency set cannot borrow a sealed manifest.
                from . import validation
                cache=getattr(self.service,'_verified_json_reads',None)
                if cache is None:
                    value=json.loads(data);validation.validate_record('knowledge_batch',value)
                else:
                    value=cache.captured(batch,data,kind='knowledge_batch',validator=validation.validate_record,
                        project=lambda v:{'task_ids':v['task_ids']},projection_key=('scan_batch_tasks',),origin='graph.batch_tasks')
                if value['task_ids']!=self.manifest['task_ids']:fail('batch task membership changed')
                h=hashlib.sha256()
                for path in sorted(records):
                    encoded=str(path).encode();h.update(len(encoded).to_bytes(8,'big'));h.update(encoded);h.update(records[path][1])
                trace.update(files=len(records),mutable_bytes=len(data),static_files=len(paths),generation=self.generation)
                return {'digest':h.digest(),'records':records,'files':len(records),'bytes':len(data),
                    'directories':self.manifest['directories'],
                    'proof_files':{str(p):digest.hex() for p,(_,digest) in records.items()}}

    def recheck(self,proof):
        from .read_accounting import _raw_read_bytes
        from .runtime_telemetry import span
        from . import validation
        batch=self.manifest['batch'];static=[p for p in proof['files'] if p!=batch]
        if any(p not in self.manifest['files'] for p in static):return False
        with span('snapshot_dependency_recheck',vault=self.service.vault,generation=self.generation) as trace:
            with self.guard([*static,*self.manifest['directories']]):
                if proof['directories']!=self.manifest['directories']:fail('dependency proof membership differs')
                schema=Path(validation.__file__).with_name('schemas')/'contracts.json'
                if hashlib.sha256(_raw_read_bytes(schema,'dependency_recheck.schema')).hexdigest()!=proof['schema_sha256']:fail('Build schema changed')
                for name,digest in proof['files'].items():
                    actual=hashlib.sha256(_raw_read_bytes(Path(name),'build_dependency_recheck.batch')).hexdigest() if name==batch else self.manifest['files'][name]
                    if actual!=digest:fail('dependency proof bytes differ')
                trace.update(static_files=len(static),avoided_bytes=sum(self.manifest['content_identity'][p][3] for p in static))
        return True


def records_as_strings(records):return {str(p) for p in records}


def current():return None if _BYPASS.get() else _SESSION.get()


@contextmanager
def snapshot_scope(service,*,directory=None,restrict=True):
    if _SESSION.get() is not None:yield _SESSION.get();return
    if directory is None:
        if os.name!='posix' or not os.environ.get('HERMES_KANBAN_TASK'):yield None;return
        wid=os.environ.get('HERMES_EVIDENCE_WORKFLOW')
        roots=[service.vault/ROOT/wid/'state.json'] if wid else list((service.vault/ROOT).glob('*/state.json'))
        roots=[p for p in roots if p.exists()]
        active=[p.parent for p in roots if wid or json.loads(p.read_text()).get('state')=='active']
        if not active:yield None;return
        if len(active)!=1:fail('Vault has multiple active evidence protectors')
        directory=active[0]
    session=EvidenceSession(service,Path(directory),restrict=restrict);token=_SESSION.set(session)
    from .runtime_telemetry import span
    try:yield session
    finally:
        with span('evidence_snapshot_summary',vault=service.vault,generation=session.generation,
                  **session.stats,sources=session.sources):pass
        _SESSION.reset(token)


@contextmanager
def full_evidence_reads():
    session=_SESSION.get()
    # Keep a short reader lease for the original full write-locked commit.
    # The protector cannot acknowledge revocation half way through that commit.
    with session.guard() if session else _empty():
        token=_BYPASS.set(True)
        try:yield
        finally:_BYPASS.reset(token)


@contextmanager
def _empty():yield


def guard_write(path):
    """Known atomic writer requests revoke before attempting a protected write."""
    if os.name!='posix':return
    path=Path(path).absolute()
    try:position=path.parts.index('_system')
    except ValueError:return
    vault=Path(*path.parts[:position]);root=vault/ROOT
    if not root.exists():return
    for record in root.glob('*/state.json'):
        state=json.loads(record.read_text())
        if state.get('state')!='active':continue
        # Only a protected target needs the broker; new dynamic files and all
        # workflow/journal/audit/budget records retain their original writes.
        target=path
        while not os.path.lexists(target):target=target.parent
        if not flags(target)&IMMUTABLE:continue
        result=rpc(state['socket'],{'operation':'write','path':str(path)})
        if result.get('covered'):fail('protected write requested revocation; stop affected consumers before controlled retry')
