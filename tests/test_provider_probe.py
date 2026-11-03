import json
from types import SimpleNamespace
from pathlib import Path
import pytest
import provider_probe as p
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint

@pytest.fixture
def probe(tmp_path,monkeypatch):
    root=tmp_path/'vault';root.mkdir();out=tmp_path/'audit';out.mkdir()
    host=tmp_path/'host.json';host.write_text('{}')
    paths=[tmp_path/'write.json',tmp_path/'query.json']
    cfg={'enabled':False,'provider_config':str(host)}
    for path in paths:path.write_text(json.dumps(cfg))
    manifest=root/'_system/reports/retrieval-index-manifest.json';manifest.parent.mkdir(parents=True);manifest.write_text('{"status":"disabled"}')
    w={'actor':'operator','revision':1,'cancel_requested':False,'release_id':'release-isolated',
       'pause_control':{'boundary':'release_sync','evidence_digest':'pause'}}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=root,status=lambda _:w,_verify_pause_snapshot=lambda _:None))
    monkeypatch.setattr(p,'config_paths',lambda:paths)
    monkeypatch.setattr(p,'provider_module',lambda:SimpleNamespace(provider_config=lambda _:(paths[0],cfg),release_identity=lambda _:('release-isolated','hash')))
    q={'contract':p.CONTRACT,'sync_once':True,'preserve_pause':True,'keep_disabled':True,'operation_id':'isolated',
       'workflow_id':'ingest-isolated','actor':'operator','expected_revision':1,'pause_digest':'pause',
       'release_id':'release-isolated','release_hash':'hash','adapter_hashes':{str(x):p.sha(x) for x in paths},
       'host_config_sha256':p.sha(host),'disabled_manifest_sha256':p.sha(manifest),'output_dir':str(out)}
    q['input_digest']='sha256:'+fingerprint(q)
    return adapter,q,w,paths

def test_probe_only_admits_exact_disabled_pause_without_mutation(probe):
    adapter,q,w,paths=probe;before=[x.read_bytes() for x in paths]
    assert p.check(adapter,q)[0] is w
    assert before==[x.read_bytes() for x in paths]

@pytest.mark.parametrize('change',['authority','config','release','pause','manifest'])
def test_probe_rejects_changed_grant_or_inputs(probe,change):
    adapter,q,w,paths=probe
    if change=='authority':q['sync_once']=False;q['input_digest']='sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})
    elif change=='config':paths[1].write_text('{"enabled":true}')
    elif change=='release':w['release_id']='different'
    elif change=='pause':w['pause_control']['boundary']=None
    else:(adapter.workflow.vault/'_system/reports/retrieval-index-manifest.json').write_text('{"status":"ready"}')
    with pytest.raises(ContractError):p.check(adapter,q)
