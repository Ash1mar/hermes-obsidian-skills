import hashlib,json
from types import SimpleNamespace
from contextlib import nullcontext
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.provider_disposition import active,validate_manifest,SCOPE

@pytest.fixture
def disabled(tmp_path):
    w={'workflow_id':'ingest-isolated','actor':'operator','scope':{'provider':'sync'},'release_id':'release-isolated'}
    release={'ref':'_system/knowledge-releases/release-isolated/manifest.json','sha256':hashlib.sha256(b'{}').hexdigest()}
    path=tmp_path/release['ref'];path.parent.mkdir(parents=True);path.write_bytes(b'{}')
    config={'enabled':False,'transport':'command','command':['must-never-run']}
    q={'workflow_id':w['workflow_id'],'actor':w['actor'],'recovery_id':'skip-1','recovery_scope':SCOPE,
       'skip_provider_sync':True,'stop_after':'release_sync','adapter_config_fingerprint':fingerprint(config),'release':release}
    q['input_digest']='sha256:'+fingerprint(q)
    report={'request':q,'entries':[{'node':'provider-sync','previous_task_id':'native-ended',
        'ended_run_ids':['1'],'failure':{'error_code':'PROVIDER_DISABLED'}}],
        'domain_calls_granted':0,'provider_calls_granted':0,'index_created':False}
    data=_json_bytes(report);ref='_system/disposition.json';(tmp_path/ref).write_bytes(data)
    w['execution_recovery_history']=[{'nodes':['provider-sync'],'recovery_id':'skip-1','input_digest':q['input_digest'],
        'report_ref':ref,'report_sha256':hashlib.sha256(data).hexdigest()}]
    return tmp_path,w,config,report

def test_unapproved_disabled_stays_blocked_and_authorized_worker_never_calls_provider(disabled,monkeypatch):
    import program_worker as worker
    root,w,config,report=disabled
    module=worker.provider_module()
    monkeypatch.setattr(module,'provider_config',lambda _:('unused',config))
    monkeypatch.setattr(module,'call_command',lambda *a:pytest.fail('disabled Provider called'))
    monkeypatch.setattr(module,'call_http',lambda *a:pytest.fail('disabled HTTP called'))
    monkeypatch.setattr(worker,'provider_module',lambda:module)
    monkeypatch.setattr(worker,'worker_binding',lambda *a,**k:nullcontext())
    monkeypatch.setattr(worker,'workflow_write_guard',lambda *a,**k:nullcontext())
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=root),worker_check=lambda q:None)
    with pytest.raises(ContractError,match='PROVIDER_DISABLED'):
        worker.sync_provider(adapter,{}, {**w,'execution_recovery_history':[]})
    assert not (root/'_system/reports/retrieval-index-manifest.json').exists()
    worker.sync_provider(adapter,{},w)
    manifest=json.loads((root/'_system/reports/retrieval-index-manifest.json').read_text())
    assert validate_manifest(root,w,manifest,config=config)['provider_executed'] is False
    assert manifest['status']=='disabled' and manifest['last_success'] is None and not manifest['query_ready']

@pytest.mark.parametrize('change',['release','actor','enabled','audit'])
def test_disabled_authority_rejects_changed_inputs(disabled,change):
    root,w,config,report=disabled
    if change=='release':(root/report['request']['release']['ref']).write_text('changed')
    elif change=='actor':w['actor']='another-operator'
    elif change=='enabled':config['enabled']=True
    else:(root/w['execution_recovery_history'][0]['report_ref']).write_text('{}')
    with pytest.raises(ContractError):active(root,w,config=config)

@pytest.mark.parametrize('field,value',[('status','ready'),('query_ready',True),('index_generation','fake'),('document_count',1)])
def test_disabled_cannot_claim_ready_or_index(disabled,field,value):
    root,w,config,_=disabled;proof=active(root,w,config=config)
    manifest={'status':'disabled','sync_disposition':proof,'release_id':w['release_id'],'release_hash':proof['release_hash'],
        'query_ready':False,'index_generation':None,'document_count':0,'chunk_count':0,'transport':'disabled','errors':[]}
    validate_manifest(root,w,manifest,config=config)
    manifest[field]=value
    with pytest.raises(ContractError):validate_manifest(root,w,manifest,config=config)
