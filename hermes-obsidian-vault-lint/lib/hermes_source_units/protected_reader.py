"""A capability-free content reader for privileged coordinating processes.

The parent retains its write/process duties. Every cache use requires a fresh
guard in this separate reader; only immutable bytes and their digest return.
No execution decisions, writes, model calls or native API are exposed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
import base64,json,os,select,struct,subprocess,sys,threading,time
from .validation import ContractError

CONTRACT='hermes-protected-reader/v1'
MAX_FRAME=96*1024*1024
_CLIENT=ContextVar('coordinator_protected_reader',default=None)
_RAW=ContextVar('coordinator_full_content_reads',default=False)


def fail(message):raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$',message)


def current():
    from .evidence_snapshot import _BYPASS
    return None if _RAW.get() or _BYPASS.get() else _CLIENT.get()


@contextmanager
def fresh_content_reads():
    """Disable this optimization at mutation/commit boundaries, not worker gates."""
    token=_RAW.set(True)
    try:yield
    finally:_RAW.reset(token)


def _send(value):
    data=json.dumps(value,separators=(',',':')).encode()
    if len(data)>MAX_FRAME:fail('protected reader response exceeds budget')
    sys.stdout.buffer.write(struct.pack('!Q',len(data))+data);sys.stdout.buffer.flush()


def serve(vault,directory,roles):
    from .evidence_snapshot import EvidenceSession
    from .read_accounting import accounting_scope
    service=SimpleNamespace(vault=Path(vault))
    with accounting_scope() as ledger:
        session=EvidenceSession(service,Path(directory),restrict=True)
        allowed=frozenset(name for role in roles for name in session.manifest.get(role,{}))
        if any(name not in session.manifest['files'] for name in allowed):fail('invalid protected reader asset roles')
        status=Path('/proc/self/status').read_text().splitlines()
        caps={line.split(':')[0]:line.split(':')[1].strip() for line in status
            if line.split(':')[0] in ('CapEff','CapPrm','CapBnd','CapAmb','NoNewPrivs')}
        _send({'contract':CONTRACT,'generation':session.generation,'pid':os.getpid(),'capabilities':caps,
            'allowed':sorted(allowed)})
        previous={};previous_hash={}
        def delta():
            io=[];hashes=[]
            for key,row in ledger.rows.items():
                old=previous.get(key,{'reads':0,'bytes':0})
                if row['reads']!=old['reads'] or row['bytes']!=old['bytes']:
                    io.append({**{k:row[k] for k in ('path','origin')},
                        'reads':row['reads']-old['reads'],'bytes':row['bytes']-old['bytes']})
                previous[key]={'reads':row['reads'],'bytes':row['bytes']}
            for key,row in ledger.hash_rows.items():
                old=previous_hash.get(key,{'hash_calls':0,'hash_bytes':0,'hash_ns':0})
                if row['hash_calls']!=old['hash_calls']:
                    hashes.append({**{k:row[k] for k in ('path','origin')},
                        **{k:row[k]-old[k] for k in ('hash_calls','hash_bytes','hash_ns')}})
                previous_hash[key]={k:row[k] for k in ('hash_calls','hash_bytes','hash_ns')}
            return {'io':io,'hashes':hashes}
        while True:
            raw=sys.stdin.buffer.readline(16385)
            if not raw:return  # Parent exit closes its private pipe.
            if len(raw)>16384:fail('protected reader request exceeds budget')
            request=json.loads(raw);identity=request['id']
            try:
                if request['operation']=='close':
                    _send({'id':identity,'generation':session.generation,'costs':dict(session.costs),
                        'stats':dict(session.stats),**delta()});return
                if request['operation']!='read':fail('unsupported protected reader operation')
                name=request['path']
                if name not in allowed:fail('protected reader path outside declared asset roles')
                if session.manifest['content_identity'][name][3]>64*1024*1024:fail('protected asset exceeds content budget')
                data,digest=session.read_hashed(Path(name),'protected_reader.content')
                _send({'id':identity,'generation':session.generation,'path':name,'digest':digest,
                    'data':None if request.get('known_digest')==digest else base64.b64encode(data).decode(),**delta()})
            except ContractError as exc:
                _send({'id':identity,'generation':session.generation,'error':{'code':exc.code,'message':str(exc)},**delta()})


class ProtectedReader:
    def __init__(self,service,directory,roles):
        from .evidence_snapshot import ROOT
        self.service=service;self.directory=Path(directory);self.process=None;self.serial=0;self.cache={};self.lock=threading.RLock()
        self.stats={'requests':0,'first_payloads':0,'payload_bytes':0,'response_bytes':0,'request_ns':0}
        library=Path(__file__).resolve().parent.parent
        env=dict(os.environ,PYTHONPATH=str(library),HERMES_PROTECTED_READER_WORKFLOW=self.directory.name)
        self.process=subprocess.Popen([sys.executable,'-m','hermes_source_units.protected_reader',
            '--vault',str(service.vault),'--directory',str(directory),'--roles',','.join(roles)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,cwd=library,bufsize=0)
        try:
            hello=self._receive()
            if hello.get('error'):
                error=hello['error'];raise ContractError(error['code'],'$',error['message'])
            if hello.get('contract')!=CONTRACT or hello['pid']!=self.process.pid:fail('protected reader handshake mismatch')
            caps=hello['capabilities']
            if any(int(caps[k],16) for k in ('CapEff','CapPrm','CapBnd','CapAmb')) or caps['NoNewPrivs']!='1':
                fail('protected reader retains privileges')
            self.generation=hello['generation'];self.allowed=frozenset(hello['allowed']);self.pid=self.process.pid
        except BaseException:self.close(abort=True);raise

    def _receive(self):
        deadline=time.monotonic()+30;fd=self.process.stdout.fileno()
        def exact(count):
            parts=[]
            while count:
                left=deadline-time.monotonic()
                if left<=0 or not select.select([fd],[],[],left)[0]:fail('protected reader response timed out')
                part=os.read(fd,min(count,65536))
                if not part:fail('protected reader exited')
                parts.append(part);count-=len(part)
            return b''.join(parts)
        size=struct.unpack('!Q',exact(8))[0]
        if size>MAX_FRAME:fail('protected reader frame exceeds budget')
        self.stats['response_bytes']+=size+8
        return json.loads(exact(size))

    def _request(self,operation,**fields):
        self.serial+=1;identity=self.serial;started=time.monotonic_ns()
        try:
            self.process.stdin.write(json.dumps({'id':identity,'operation':operation,**fields}).encode()+b'\n')
            self.process.stdin.flush();result=self._receive()
        except (BrokenPipeError,OSError,ValueError) as exc:fail('protected reader unavailable: '+str(exc))
        finally:self.stats['request_ns']+=time.monotonic_ns()-started
        if result.get('id')!=identity or result.get('generation')!=self.generation:fail('protected reader response identity mismatch')
        from .read_accounting import _LEDGER
        ledger=_LEDGER.get()
        if ledger is not None:
            for row in result.get('io',[]):ledger.note(row['path'],row['bytes'],'protected_reader:'+row['origin'],count=row['reads'])
            for row in result.get('hashes',[]):
                for _ in range(row['hash_calls']):ledger.note_hash(row['path'],'protected_reader:'+row['origin'],
                    size=row['hash_bytes']//row['hash_calls'],elapsed=row['hash_ns']//row['hash_calls'])
        if result.get('error'):
            error=result['error'];raise ContractError(error['code'],'$',error['message'])
        return result

    def read_hashed(self,path,origin):
        name=str(path)
        if name not in self.allowed:return None
        with self.lock:
            known=self.cache.get(name);result=self._request('read',path=name,known_digest=known[1] if known else None)
            if result.get('path')!=name:fail('protected reader path mismatch')
            if result['data'] is not None:
                data=base64.b64decode(result['data'],validate=True)
                self.cache[name]=(data,result['digest']);self.stats['first_payloads']+=1;self.stats['payload_bytes']+=len(data)
            elif known is None or known[1]!=result['digest']:fail('protected reader omitted unknown content')
            self.stats['requests']+=1
            from .read_accounting import _LEDGER
            ledger=_LEDGER.get()
            if ledger is not None:
                ledger.note(path,0,'protected_reader.reuse:'+origin,captured=True)
                ledger.note_hash(path,'protected_reader.reuse:'+origin,reused=True)
            return self.cache[name]

    def close(self,*,abort=False):
        process=self.process
        if process is None:return
        try:
            if not abort and process.poll() is None:
                result=self._request('close')
                from .runtime_telemetry import span
                with span('coordinator_protected_reader',vault=self.service.vault,workflow_id=self.directory.name,
                        reader_pid=process.pid,generation=self.generation,**self.stats,
                        snapshot_stats=result['stats'],computation_costs=result['costs']):pass
        finally:
            process.stdin.close()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            process.stdout.close();self.process=None


@contextmanager
def coordinator_reads(service,workflow_id,*,roles=('workflow_segments','host_files'),enabled=True):
    """Opt-in immutable assets, not a worker environment or authority scope."""
    if not enabled or os.name!='posix' or _CLIENT.get() is not None:
        yield;return
    from .evidence_snapshot import ROOT,current as worker_session
    if worker_session() is not None:yield;return
    if Path(workflow_id).name!=workflow_id or workflow_id in ('.','..'):fail('invalid protected reader workflow scope')
    directory=service.vault/ROOT/workflow_id;state=directory/'state.json'
    from .read_accounting import _raw_read_bytes
    try:active=json.loads(_raw_read_bytes(state,'protected_reader.admission'))['state']=='active'
    except (FileNotFoundError,ValueError,KeyError):active=False
    if not active:yield;return  # No caching; the original full read/hash remains.
    client=ProtectedReader(service,directory,roles);token=_CLIENT.set(client)
    try:yield client
    finally:
        _CLIENT.reset(token)
        # Preserve an original error if cleanup of an already failed broker fails.
        client.close(abort=sys.exc_info()[0] is not None)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--vault',required=True);p.add_argument('--directory',required=True)
    p.add_argument('--roles',required=True);a=p.parse_args()
    try:serve(a.vault,a.directory,a.roles.split(','))
    except Exception as exc:
        try:_send({'error':{'code':getattr(exc,'code','RUNTIME_UNVERIFIED'),'message':str(exc)}})
        finally:sys.exit(1)
