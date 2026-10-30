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
