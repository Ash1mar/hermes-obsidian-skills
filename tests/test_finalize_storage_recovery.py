"""Only a recorded mechanical output seal conflict may recover; never source writes."""
import copy
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import build_execution_recovery as recovery
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.partial_finalize import pin
from hermes_source_units.finalize_revision_state import append,work_id
from hermes_source_units.evidence_snapshot import ROOT

def setup(tmp_path,monkeypatch):
    binding={'workflow_id':'fixture','task_id':'old','actor':'agent','node':'build-finalize'}
    root=tmp_path/ROOT/'fixture';root.mkdir(parents=True)
    manifest={'generation':'generation','original_flags':{str(tmp_path/'unchanged-source'):0}}
    (root/'manifest.json').write_bytes(_json_bytes(manifest))
    state={'state':'revoked','workflow_id':'fixture','generation':'generation',
        'reason':'controlled write requested: '+str(tmp_path/'_system/knowledge-builds/.finalize.lock'),
        'manifest_sha256':pin(tmp_path,(root/'manifest.json').relative_to(tmp_path).as_posix())['sha256']}
    (root/'state.json').write_bytes(_json_bytes(state))
    output=tmp_path/'_system/knowledge-builds';output.mkdir(parents=True)
    (output/'page-revisions').mkdir()
    append(tmp_path,'fixture',{'work_id':work_id('run','*'),'run_id':'run','page_id':'*','state':'enabled',
        'storage_prepared':['_system/knowledge-builds/.finalize.lock','_system/knowledge-builds/page-revisions']},'prepare','prepare-storage',{})
    snapshot=tmp_path/'snapshot.json';snapshot.write_bytes(_json_bytes({'backend':{'runs':[{'run_id':'run'}]}}))
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:{'state':'cancelled'}))
    failure={'error_code':'EVIDENCE_SNAPSHOT_REVOKED',
        'reason':'EVIDENCE_SNAPSHOT_REVOKED at $: protected write requested revocation; stop affected consumers before controlled retry'}
    entry={'snapshot_ref':'snapshot.json','retry_unknowns':[]}
    monkeypatch.setattr('hermes_source_units.evidence_snapshot.flags',lambda _:0)
    return adapter,binding,failure,entry,root,state

def test_storage_recovery_retains_old_registry_data_and_grants_zero_calls(tmp_path,monkeypatch):
    a,b,f,e,root,state=setup(tmp_path,monkeypatch)
    proof=recovery.storage_interruption(a,b,f,e)
    assert proof['new_domain_calls_granted']==0 and proof['source_protection_unchanged']
    (root/'state.json').write_text('{"generation":"later-generation"}')
    assert proof['protector_state']==state

@pytest.mark.parametrize('change',['source-path','reason','generation','flags','grant','layout'])
def test_storage_recovery_cannot_hide_source_changes_or_grant_unknown_retries(tmp_path,monkeypatch,change):
    a,b,f,e,root,state=setup(tmp_path,monkeypatch)
    if change=='source-path':state['reason']='controlled write requested: '+str(tmp_path/'_system/sources/original.json')
    elif change=='reason':f['reason']='reader schema changed'
    elif change=='generation':state['generation']='different'
    elif change=='flags':monkeypatch.setattr('hermes_source_units.evidence_snapshot.flags',lambda _:16)
    elif change=='grant':e['retry_unknowns']=[{'reason':'new call'}]
    else:(tmp_path/'_system/knowledge-builds/.finalize.lock').touch()
    (root/'state.json').write_bytes(_json_bytes(state))
    with pytest.raises(ContractError):recovery.storage_interruption(a,b,f,e)

def empty_setup(tmp_path,monkeypatch):
    a,b,f,e,root,state=setup(tmp_path,monkeypatch)
    from hermes_source_units.finalize_revision_state import load
    controls=load(tmp_path,'fixture');work=copy.deepcopy(next(iter(controls['works'].values())))
    work['repaired_empty_lock']={'ref':'_system/knowledge-builds/.finalize.lock',
        'sha256':__import__('hashlib').sha256(b'').hexdigest(),'previous_control_head':controls['head']}
    append(tmp_path,'fixture',work,'repair','prepare-storage',{})
    e['snapshot_sha256']=pin(tmp_path,'snapshot.json')['sha256']
    f={'error_code':'LOCK_RECOVERY_REQUIRED','reason':f'LOCK_RECOVERY_REQUIRED at $: Legacy or foreign-host lock requires supported recovery: {tmp_path/work["repaired_empty_lock"]["ref"]}'}
    return a,b,f,e

def test_recover_only_authenticated_empty_preparation_artifact(tmp_path,monkeypatch):
    a,b,f,e=empty_setup(tmp_path,monkeypatch)
    assert recovery.empty_lock_interruption(a,b,f,e)['new_domain_calls_granted']==0

@pytest.mark.parametrize('change',['foreign-path','reason','retry','existing-marker','missing-directory','unbound-snapshot'])
def test_empty_lock_recovery_does_not_allow_unrelated_failures(tmp_path,monkeypatch,change):
    a,b,f,e=empty_setup(tmp_path,monkeypatch)
    if change=='foreign-path':f['reason']=f['reason'].replace('.finalize.lock','foreign.lock')
    elif change=='reason':f['error_code']='SOURCE_CHANGED'
    elif change=='retry':e['retry_unknowns']=[{}]
    elif change=='existing-marker':(tmp_path/'_system/knowledge-builds/.finalize.lock').write_text('foreign')
    elif change=='missing-directory':(tmp_path/'_system/knowledge-builds/page-revisions').rmdir()
    else:e['snapshot_sha256']='0'*64
    with pytest.raises(ContractError):recovery.empty_lock_interruption(a,b,f,e)
