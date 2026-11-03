"""Admit a stopped, exact disabled Provider attempt as an explicit no-index outcome."""
import hashlib,json,os,re
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock,_vault_path,_load_json,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint,record_validation_scope
from hermes_source_units.provider_disposition import SCOPE,active
from native_stop_inventory import verify_cancel_drain

def prepare(adapter,request,*,dry_run=False):
    def fail(message):raise ContractError('PROVIDER_DISPOSITION_INVALID','$',message)
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):fail('operator only')
    service=adapter.workflow;service._check_request(request);wid=request['workflow_id'];rid=request.get('recovery_id','')
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',rid) or not request.get('reason','').strip():fail('stable operation and reason required')
    with _exclusive_lock(_vault_path(service.vault,f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')),_exclusive_lock(service._lock(wid)),record_validation_scope():
        w=service.status(wid);service.assert_execution_ready(w)
        if request['actor']!=w['actor']:fail('actor differs')
        for h in w.get('execution_recovery_history',[]):
            if h['recovery_id']==rid:
                if h['input_digest']!=request['input_digest']:fail('operation conflict')
                active(service.vault,w)
                return {'ok':True,'replayed':True,'resumed':False,'report_ref':h['report_ref']}
        w=service._mutation(request)
        if (request.get('recovery_scope')!=SCOPE or request.get('skip_provider_sync') is not True
                or request.get('stop_after')!='release_sync' or w['state']!='cancelled' or not w['cancel_requested']
                or w['current_stage']!='indexing' or service.pending_pause(w) or w['scope'].get('provider')!='sync'
                or 'release_sync' not in w['scope'].get('pause_after',[])):fail('requires explicit stopped disabled disposition before release_sync')
        verify_cancel_drain(adapter,wid,timeout=0)
        if any('provider-sync' in h['nodes'] for h in w.get('execution_recovery_history',[])):fail('Provider disposition already admitted')
        from ingest_kanban import desired_graph
        node=next(n for n in desired_graph(service,w) if n.name=='provider-sync')
        card=next(c for c in w['kanban']['task_map'] if c['node']==node.name)
        if request.get('task_id')!=card['task_id'] or request.get('input_fingerprint')!=node.input_fingerprint:fail('current native binding differs')
        key=f'ingest:{wid}:provider-sync:{node.input_fingerprint}'
        if card['idempotency_key']!=key:fail('card identity differs')
        binding=_load_json(_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json'))
        if any(binding.get(k)!=v for k,v in {'workflow_id':wid,'node':node.name,'task_id':card['task_id'],'actor':w['actor'],'input_fingerprint':node.input_fingerprint}.items()):fail('canonical binding differs')
        failure_path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(node.name)[:16]}.json')
        data=failure_path.read_bytes();failure=json.loads(data)
        if (hashlib.sha256(data).hexdigest()!=request.get('failure_sha256') or failure.get('task_id')!=card['task_id']
                or failure.get('input_fingerprint')!=node.input_fingerprint or failure.get('error_code')!='PROVIDER_DISABLED'):
            fail('exact disabled failure required; unknown execution is not skippable here')
        native=adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],card['task_id'])
        if (native['task'].get('worker_pid') or not native['runs'] or any(not r.get('ended_at') for r in native['runs'])
                or not any(r.get('worker_pid') for r in native['runs'])):fail('exact native attempt must be ended')
        from program_worker import provider_module
        module=provider_module();_,config=module.provider_config(None)
        if config.get('enabled') is not False or fingerprint(config)!=request.get('adapter_config_fingerprint'):fail('disabled adapter differs')
        release_id,release_hash=module.release_identity(service.vault)
        if request.get('release')!={'ref':f'_system/knowledge-releases/{release_id}/manifest.json','sha256':release_hash} or release_id!=w['release_id']:fail('release changed')
        entry={'node':'provider-sync','previous_task_id':card['task_id'],'previous_input_fingerprint':node.input_fingerprint,
               'failure':failure,'failure_sha256':hashlib.sha256(data).hexdigest(),'ended_run_ids':[str(r['id']) for r in native['runs']]}
        report={'request':dict(request),'entries':[entry],'native_runs':native['runs'],'domain_calls_granted':0,
                'provider_calls_granted':0,'index_created':False,'resumed':False}
        ref=f'_system/ledgers/ingest-workflows/{wid}/provider-dispositions/{rid}.json';payload=_json_bytes(report)
        row={'recovery_id':rid,'input_digest':request['input_digest'],'report_ref':ref,'report_sha256':hashlib.sha256(payload).hexdigest(),'nodes':['provider-sync']}
        candidate={**w,'execution_recovery_history':[*w.get('execution_recovery_history',[]),row]}
        result={'ok':True,'applied':not dry_run,'resumed':False,'provider_executed':False,'index_created':False,'stop_after':'release_sync','report_ref':ref}
        if dry_run:return result
        path=_vault_path(service.vault,ref)
        if path.exists() and path.read_bytes()!=payload:fail('immutable disposition conflict')
        _write_atomic(path,payload);active(service.vault,candidate,config=config)
        candidate['revision']+=1;service._write(candidate);return result
