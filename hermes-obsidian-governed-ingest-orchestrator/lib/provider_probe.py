"""One native Provider experiment; production adapters and workflow pause stay closed."""
import contextlib,hashlib,json,os,re,signal,subprocess,sys,tempfile,threading,time
from pathlib import Path
from hermes_source_units.source_units import _exclusive_lock,_json_bytes,_load_json,_write_atomic
from hermes_source_units.validation import fingerprint,ContractError
from ingest_kanban import IngestKanbanAdapter
from native_stop_inventory import verify_pause_drain
from program_worker import provider_module

CONTRACT='hermes-provider-probe/v1'
ASSIGNEE='ingest-program'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def fail(message):raise ContractError('PROVIDER_PROBE_INVALID','$',message)
def config_paths():
    root=Path(__file__).resolve().parents[2]
    return [root/name/'config/retrieval-provider.json' for name in
            ('hermes-obsidian-knowledge-finalize','hermes-obsidian-controlled-query')]

def check(adapter,q):
    if (q.get('contract')!=CONTRACT or q.get('sync_once') is not True or q.get('preserve_pause') is not True
            or q.get('keep_disabled') is not True or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',q.get('operation_id',''))
            or q.get('input_digest')!='sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})):
        fail('explicit one-sync authority required')
    w=adapter.workflow.status(q['workflow_id'])
    if (w['actor']!=q['actor'] or w['revision']!=q['expected_revision'] or w['cancel_requested']
            or w['pause_control']['boundary']!='release_sync' or w['pause_control']['evidence_digest']!=q['pause_digest']):
        fail('exact stopped release_sync evidence required')
    adapter.workflow._verify_pause_snapshot(w)
    module=provider_module();release_id,release_hash=module.release_identity(adapter.workflow.vault)
    if (release_id!=w['release_id'] or q['release_id']!=release_id or q['release_hash']!=release_hash):fail('release changed')
    paths=config_paths()
    if q.get('adapter_hashes')!={str(p):sha(p) for p in paths} or any(_load_json(p).get('enabled') is not False for p in paths):
        fail('both production adapters must stay disabled')
    _,cfg=module.provider_config(None)
    if cfg!=_load_json(paths[0]):fail('ambient adapter override is not allowed')
    host=Path(cfg['provider_config']).resolve()
    if q.get('host_config_sha256')!=sha(host):fail('Provider model/configuration changed')
    manifest=adapter.workflow.vault/'_system/reports/retrieval-index-manifest.json'
    if sha(manifest)!=q['disabled_manifest_sha256'] or _load_json(manifest).get('status')!='disabled':fail('disabled workflow manifest changed')
    out=Path(q['output_dir']).resolve()
    if not out.is_dir() or out.is_relative_to(adapter.workflow.vault.resolve()):fail('probe evidence belongs outside the Vault')
    return w,cfg,out

def dispatch(vault,request_path):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):fail('trusted operator required')
    from hermes_cli import kanban_db as kb,kanban_db_connect as kc,kanban_db_dispatch as kd
    adapter=IngestKanbanAdapter(vault);q=_load_json(request_path);w,_,out=check(adapter,q)
    lock=adapter.workflow.vault/'_system/ledgers/ingest-workflows/.dispatch-locks/program-capacity.lock'
    with _exclusive_lock(lock):
        verify_pause_drain(adapter,q['workflow_id'],timeout=0)
        board=w['kanban']['board_id']
        body={'contract':CONTRACT,'request_path':str(Path(request_path).resolve()),'request_sha256':sha(request_path)}
        with contextlib.closing(kc.connect(board=board)) as conn:
            if kd.count_running_tasks(conn)+kd.count_running_tasks_other_boards(board):fail('probe requires idle native capacity')
            if kd._memory_pressure_level()=='critical':fail('host memory pressure')
            tid=kb.create_task(conn,title='One-time release Provider experiment; production disabled',body=json.dumps(body,sort_keys=True),
                assignee=ASSIGNEE,created_by='provider-probe',workspace_kind='dir',workspace_path=str(out),
                project_id='',initial_status='ready',max_retries=1,max_runtime_seconds=1900,
                idempotency_key='provider-probe:'+q['workflow_id']+':'+q['operation_id'],board=board)
            task=kb.get_task(conn,tid)
            if task.body!=json.dumps(body,sort_keys=True):fail('operation identity already differs')
            if task.status!='ready':return {'task_id':tid,'state':task.status,'spawned':False}
            claimed=kb.claim_task(conn,tid,claimer='provider-probe-dispatch')
            if claimed is None:fail('native claim unavailable')
            script=Path(__file__).resolve().parents[1]/'scripts/probe_release_provider.py'
            env=dict(os.environ,HERMES_KANBAN_TASK=tid,HERMES_KANBAN_BOARD=board,
                HERMES_KANBAN_RUN_ID=str(claimed.current_run_id),HERMES_KANBAN_CLAIM_LOCK=claimed.claim_lock,
                HERMES_INGEST_WORKFLOW_ID=q['workflow_id'])
            try:
                argv=kd._restart_safe_worker_argv(claimed,[sys.executable,str(script),'worker','--vault',str(vault),'--request',str(request_path)])
                with kd._open_worker_log(claimed,board) as log:
                    child=subprocess.Popen(argv,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
                kd._set_worker_pid(conn,tid,child.pid)
            except Exception as exc:
                kd._record_task_failure(conn,tid,str(exc),outcome='spawn_failed',force_trip=True,release_claim=True,end_run=True)
                raise
            result={'task_id':tid,'board':board,'run_id':claimed.current_run_id,'worker_pid':child.pid,'spawned':True}
            _write_atomic(out/'native-dispatch.json',_json_bytes(result));return result

def worker(vault,request_path):
    from hermes_cli import kanban_db as kb,kanban_db_connect as kc,kanban_db_dispatch as kd
    adapter=IngestKanbanAdapter(vault);q=_load_json(request_path);w,cfg,out=check(adapter,q)
    tid=os.environ.get('HERMES_KANBAN_TASK');board=os.environ.get('HERMES_KANBAN_BOARD');run=int(os.environ['HERMES_KANBAN_RUN_ID'])
    if board!=w['kanban']['board_id']:fail('native board differs')
    def own(conn):
        task=kb.get_task(conn,tid)
        return (task and task.status=='running' and task.current_run_id==run and task.worker_pid==os.getpid()
            and task.claim_lock==os.environ['HERMES_KANBAN_CLAIM_LOCK'] and task.created_by=='provider-probe'
            and json.loads(task.body)=={'contract':CONTRACT,'request_path':str(Path(request_path).resolve()),'request_sha256':sha(request_path)})
    deadline=time.monotonic()+10
    with contextlib.closing(kc.connect(board=board)) as conn:
        while not own(conn):
            if time.monotonic()>deadline:fail('native grant missing')
            time.sleep(.05)
    stop=threading.Event();child=None
    def heartbeat():
        while not stop.wait(20):
            with contextlib.closing(kc.connect(board=board)) as conn:
                if not own(conn) or not kd.heartbeat_worker(conn,tid,expected_run_id=run,note='one-time sync; production disabled'):
                    os.kill(os.getpid(),signal.SIGTERM);return
    def terminated(signum,frame):raise RuntimeError('native probe stopped; preserve candidate evidence')
    old_handler=signal.signal(signal.SIGTERM,terminated);thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    started=time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='provider-one-shot-') as tmp:
            config=Path(tmp)/'enabled.json';config.write_bytes(_json_bytes({**cfg,'enabled':True}))
            script=Path(__file__).resolve().parents[2]/'hermes-obsidian-knowledge-finalize/scripts/sync_release_index.py'
            with (out/'sync.stdout.json').open('wb') as stdout,(out/'sync.stderr.log').open('wb') as stderr:
                child=subprocess.Popen([sys.executable,str(script),str(vault),'--provider-config',str(config),'--no-write-manifest'],
                    stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
                rc=child.wait(timeout=int(cfg.get('timeout_seconds',1800))+30)
            if rc:fail('Provider sync failed; inspect sync stdout/stderr and persisted progress')
        payload=_load_json(out/'sync.stdout.json');index=payload.get('index',{})
        if (index.get('status')!='ready' or index.get('release_id')!=q['release_id'] or index.get('release_hash')!=q['release_hash']
                or not index.get('index_generation') or payload.get('manifest_path') is not None):fail('exact ready experiment result required')
        check(adapter,q)
        result={'ok':True,'contract':CONTRACT,'task_id':tid,'native_run_id':run,'release_id':q['release_id'],
            'release_hash':q['release_hash'],'seconds':time.monotonic()-started,'sync_metrics':index.get('sync_metrics',{}),
            'index_generation':index['index_generation'],'document_count':index['document_count'],
            'production_adapters_disabled':True,'workflow_pause_preserved':True,'vault_manifest_written':False,
            'temporary_config_removed':True,'domain_llm_calls':0}
        _write_atomic(out/'native-result.json',_json_bytes(result))
        with contextlib.closing(kc.connect(board=board)) as conn:
            if not own(conn) or not kb.complete_task(conn,tid,expected_run_id=run,result=json.dumps(result),summary='One-time Provider sync verified; production adapters disabled; workflow pause preserved'):
                fail('native completion ownership changed')
        return result
    except Exception as exc:
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
        _write_atomic(out/'native-failure.json',_json_bytes({'ok':False,'error':str(exc),'task_id':tid,'run_id':run,'seconds':time.monotonic()-started}))
        with contextlib.closing(kc.connect(board=board)) as conn:
            if own(conn):kd._record_task_failure(conn,tid,str(exc),outcome='probe_failed',force_trip=True,release_claim=True,end_run=True)
        raise
    finally:
        stop.set();thread.join(timeout=2);signal.signal(signal.SIGTERM,old_handler)
