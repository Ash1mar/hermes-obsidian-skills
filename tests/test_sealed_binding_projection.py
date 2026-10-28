from hermes_source_units.evidence_snapshot import binding
from hermes_source_units.verified_json import VerifiedJsonReads
import json
import pytest
from hermes_source_units import ContractError
from hermes_source_units.evidence_snapshot import assert_sealable_slices


def test_sealed_binding_selects_fingerprint_without_recopying_revision(tmp_path):
    value={'workflow_id':'w','batch_id':'b','pass_revision':{'tasks':[{'large':'x'*1000}]*100},
           'execution_recovery_history':[{'report_ref':'r'}],'cancel_requested':False}
    path=tmp_path/'w.json';path.write_text(json.dumps(value));cache=VerifiedJsonReads()
    snap=cache.read(path,readonly=True)
    assert binding(snap)==binding(value)==binding(snap)
    assert not any('[pass_revision]' in origin for origin in cache.copy_origins)
    # Dynamic stop bits are intentionally observed by the original workflow
    # gates, not folded into this static-evidence-only identity.
    value['cancel_requested']=True;assert binding(snap)==binding(value)
    value['execution_recovery_history'].append({'report_ref':'changed'})
    assert binding(snap)!=binding(value)


def test_terminal_cancelled_descriptors_are_sealed_without_acceptance_or_mutation():
    w={'state':'cancelled','current_stage':'build_finalizing','checkpoints':{'checkpoint_1':{'state':'approved'}}}
    slices=[{'state':state,'lease':{'worker_id':None}} for state in ('completed','cancelled')]
    before=json.dumps(slices);assert_sealable_slices(w,slices);assert json.dumps(slices)==before


@pytest.mark.parametrize('state,worker',[('ready',None),('failed',None),('leased','worker'),('completed','worker')])
def test_open_or_leased_pass_work_is_not_sealable(state,worker):
    w={'state':'cancelled','current_stage':'build_finalizing','checkpoints':{'checkpoint_1':{'state':'approved'}}}
    with pytest.raises(ContractError):assert_sealable_slices(w,[{'state':state,'lease':{'worker_id':worker}}])
