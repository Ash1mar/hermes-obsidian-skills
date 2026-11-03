"""Explicit no-index disposition; disabled never means retrieval-ready."""
import hashlib
from .source_units import _vault_path,_load_json
from .validation import ContractError,fingerprint

SCOPE='provider-disabled/v1'

def fail(message):raise ContractError('PROVIDER_DISPOSITION_INVALID','$',message)

def active(vault,workflow,*,config=None):
    rows=[r for r in workflow.get('execution_recovery_history',[]) if r['nodes']==['provider-sync']]
    if not rows:return None
    row=rows[-1];path=_vault_path(vault,row['report_ref']);data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=row['report_sha256']:fail('disposition audit changed')
    report=_load_json(path);q=report['request'];entries=report.get('entries',[])
    if (q.get('recovery_scope')!=SCOPE or q.get('skip_provider_sync') is not True
            or q.get('stop_after')!='release_sync' or q.get('workflow_id')!=workflow['workflow_id']
            or q.get('actor')!=workflow['actor'] or q.get('recovery_id')!=row['recovery_id']
            or q.get('input_digest')!=row['input_digest']
            or q['input_digest']!='sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})
            or workflow['scope'].get('provider')!='sync'
            or len(entries)!=1 or entries[0].get('node')!='provider-sync'
            or entries[0].get('failure',{}).get('error_code')!='PROVIDER_DISABLED'
            or not entries[0].get('previous_task_id') or not entries[0].get('ended_run_ids')
            or report.get('domain_calls_granted')!=0 or report.get('provider_calls_granted')!=0
            or report.get('index_created') is not False):fail('explicit disabled authority required')
    pin=q.get('release',{});expected=f"_system/knowledge-releases/{workflow['release_id']}/manifest.json"
    if pin.get('ref')!=expected or hashlib.sha256(_vault_path(vault,expected).read_bytes()).hexdigest()!=pin.get('sha256'):
        fail('release differs from disabled authorization')
    if config is not None and (config.get('enabled') is not False or fingerprint(config)!=q.get('adapter_config_fingerprint')):
        fail('disabled adapter configuration changed')
    return {'scope':SCOPE,'authorization_ref':row['report_ref'],'authorization_sha256':row['report_sha256'],
            'release_id':workflow['release_id'],'release_hash':pin['sha256'],
            'index_created':False,'query_ready':False,'provider_executed':False}

def validate_manifest(vault,workflow,manifest,*,config=None):
    proof=active(vault,workflow,config=config)
    if (proof is None or manifest.get('status')!='disabled' or manifest.get('sync_disposition')!=proof
            or manifest.get('release_id')!=proof['release_id'] or manifest.get('release_hash')!=proof['release_hash']
            or manifest.get('query_ready') is not False or manifest.get('index_generation') is not None
            or manifest.get('document_count')!=0 or manifest.get('chunk_count')!=0
            or manifest.get('transport')!='disabled' or manifest.get('errors')!=[]):
        fail('disabled completion cannot claim an index or readiness')
    return proof
