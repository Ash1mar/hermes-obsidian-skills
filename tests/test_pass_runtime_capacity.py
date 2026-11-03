"""Capacity changes never rewrite semantic work, slice inputs or original budgets."""
import json
import pytest
from test_p3_knowledge_build import vault,plan_sliced_batch,start_and_pin,workflow_request,IngestKanbanAdapter,FakeKanban,ContractError
from hermes_source_units.pass_capacity import set_capacity,effective


def test_capacity_changes_preserve_inputs_and_detect_audit_tampering(vault):
    k=plan_sliced_batch(vault,3,'capacity')
    k._ensure_slices(k._batch('capacity'),{'pass_worker_concurrency':5})
    w=start_and_pin(vault,workflow_request(workflow_id='ingest-capacity',actor='agent',expected_revision=0,
        profile='compact-5',scope={'source_paths':[],'knowledge_selector':'all','execution_mode':'auto_full','canary_policy':'sample'},batch_id='capacity'))
    a=IngestKanbanAdapter(vault,FakeKanban(False))
    with pytest.raises(ContractError,match='INVALID_TRANSITION'):
        set_capacity(a,w['workflow_id'],10,'ten')
    a.workflow.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
    k.cancel_batch('capacity','agent',k._batch('capacity')['revision'])
    original={p:p.read_bytes() for p in a.workflow.knowledge._slice_root('capacity').glob('*.json')}
    result=set_capacity(a,w['workflow_id'],10,'ten')
    assert effective(vault,a.workflow.knowledge._batch('capacity'))==10
    assert a.workflow.knowledge._batch('capacity')['slice_config']['pass_worker_concurrency']==5
    assert all(p.read_bytes()==data for p,data in original.items())
    assert set_capacity(a,w['workflow_id'],10,'ten')['replayed']
    with pytest.raises(ContractError,match='IDEMPOTENCY_CONFLICT'):set_capacity(a,w['workflow_id'],9,'ten')
    (vault/result['request_ref']).write_text('{}')
    with pytest.raises(ContractError,match='STALE_INPUT'):effective(vault,a.workflow.knowledge._batch('capacity'))


@pytest.mark.parametrize('limit',[0,11,True])
def test_invalid_capacity_is_rejected_before_state_changes(vault,limit):
    a=IngestKanbanAdapter(vault,FakeKanban(False))
    with pytest.raises(ContractError,match='INVALID_SCHEMA'):set_capacity(a,'ingest-unused',limit,'invalid')
