"""A stopped mechanical admission resumes its saved digest, not a new allowance."""
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.source_units import _json_bytes
import finalize_delivery_flow as flow

@pytest.mark.parametrize('change',['none','actor','revision'])
def test_partial_admission_reconstructs_only_the_authenticated_original_request(tmp_path,monkeypatch,change):
    top={'workflow_id':'workflow','actor':'agent','operation_id':'operation','reason':'authorized automatic flow'}
    original={**top,'action':'authorize-program-repairs','operation_id':'operation-repair',
        'expected_revision':7,'expected_control_revision':4,'inventory':{'ref':'inventory','sha256':'a'*64}}
    original['input_digest']=mutation_digest(original)
    child={'actor':'agent','expected_revision':7,'expected_control_revision':4,'inventory':original['inventory']}
    path=tmp_path/'_system/ledgers/ingest-workflows/workflow/program-repair-operations/operation-repair/plan.json'
    path.parent.mkdir(parents=True)
    if change=='actor':child['actor']='other'
    elif change=='revision':child['expected_revision']=8
    path.write_bytes(_json_bytes({'request_digest':original['input_digest'],'requests':[child]}))
    current={**original,'expected_control_revision':5};current['input_digest']=mutation_digest(current)
    monkeypatch.setattr(flow,'operator_request',lambda *a:current.copy())
    adapter=NS(workflow=NS(vault=tmp_path))
    if change=='none':
        restored=flow.repair_request(adapter,top,tmp_path/'flow',original['inventory'])
        assert restored==original and restored['expected_control_revision']==4
    else:
        with pytest.raises(ContractError):flow.repair_request(adapter,top,tmp_path/'flow',original['inventory'])

@pytest.mark.parametrize('change',['none','digest','actor','traversal'])
def test_fresh_resume_keeps_original_admission_identity(tmp_path,change):
    original={'workflow_id':'workflow','actor':'agent','action':'run-delivery',
        'operation_id':'original','expected_revision':7,'reason':'authorized'}
    original['input_digest']=mutation_digest(original)
    root=tmp_path/'_system/ledgers/ingest-workflows/workflow/delivery-operations/original'
    root.mkdir(parents=True);(root/'request.json').write_bytes(_json_bytes(original))
    fresh={**original,'operation_id':'resume','expected_revision':12,
        'resume_of':{'operation_id':'original','request_digest':original['input_digest']}}
    if change=='digest':fresh['resume_of']['request_digest']='b'*64
    elif change=='actor':fresh['actor']='other'
    elif change=='traversal':fresh['resume_of']['operation_id']='../original'
    fresh['input_digest']=mutation_digest(fresh)
    if change=='none':
        saved,saved_root=flow.original_flow(tmp_path,fresh)
        assert saved==original and saved_root==root
        assert saved['expected_revision']==7 and fresh['expected_revision']==12
    else:
        with pytest.raises(ContractError):flow.original_flow(tmp_path,fresh)

@pytest.mark.parametrize('change',['none','missing','tamper','multiple'])
def test_recovery_uses_exact_current_owner_snapshot_not_ancestor_inventory(tmp_path,change):
    from hermes_source_units.partial_finalize import pin
    from hermes_source_units.validation import fingerprint
    root=tmp_path/'_system/ledgers/ingest-workflows/workflow'
    (root/'bindings').mkdir(parents=True);(root/'semantic-inputs').mkdir()
    binding={'task_id':'current','workflow_id':'workflow','node':'build-finalize'}
    (root/f'bindings/{fingerprint("key")}.json').write_bytes(_json_bytes(binding))
    old={'binding':{**binding,'task_id':'ancestor'}}
    (root/'old.json').write_bytes(_json_bytes(old))
    (root/'inventory.json').write_bytes(_json_bytes({'snapshot':pin(tmp_path,(root/'old.json').relative_to(tmp_path).as_posix())}))
    snapshot={'contract':'fixed-review-input/v1','binding':binding,'views':[]}
    snapshot['input_id']=fingerprint(snapshot)
    if change=='tamper':snapshot['views']=[{'changed':True}]
    if change!='missing':(root/'semantic-inputs/review-current.json').write_bytes(_json_bytes(snapshot))
    if change=='multiple':
        other={**snapshot,'views':[{'different':True}]};other.pop('input_id');other['input_id']=fingerprint(other)
        (root/'semantic-inputs/review-other.json').write_bytes(_json_bytes(other))
    (root/'reports').mkdir()
    (root/f'reports/failed-{fingerprint("build-finalize")[:16]}.json').write_bytes(_json_bytes({'error_code':'WORKFLOW_STOPPED','task_id':'current'}))
    w={'workflow_id':'workflow','kanban':{'task_map':[{'node':'build-finalize','idempotency_key':'key','task_id':'current'}]}}
    inv=pin(tmp_path,(root/'inventory.json').relative_to(tmp_path).as_posix())
    if change=='none':assert flow.recovery_basis(tmp_path,w,inv)['ref'].endswith('review-current.json')
    else:
        with pytest.raises(ContractError):flow.recovery_basis(tmp_path,w,inv)
