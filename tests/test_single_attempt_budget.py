"""Isolated admission tests; no real domain outputs or native model calls."""
import copy,json
from types import SimpleNamespace
import pytest
import fixed_semantic as f
from ingest_kanban import mechanical_failure_codes
from hermes_source_units import ContractError
from hermes_source_units.semantic_budget import PHASE_BALANCED_POLICY,budget_policy_pin
from test_phase_grouping_dependencies import grouping_fixture

def test_reserve_only_fallback_keeps_complete_input_and_output_limits():
    packet,tasks=grouping_fixture(1);before=copy.deepcopy(packet)
    usage=f.group_usage(packet,tasks,'candidate',100000,{},PHASE_BALANCED_POLICY)
    limit=usage['input_codepoints']+1
    groups,plan=f.build_group_plan(packet,tasks,'candidate',limit,{},PHASE_BALANCED_POLICY,pinned=True)
    assert groups==[tasks] and plan['state']=='ready' and packet==before
    group=plan['groups'][0]
    assert group['input_codepoints']<limit and group['semantic_attempt_limit']==1
    assert group['correction_reserve_codepoints']==0 and group['configured_correction_reserve_codepoints']==2048
    assert group['admission_mode']=='single_attempt_no_correction'
    blocked=f.build_group_plan(packet,tasks,'candidate',usage['input_codepoints']-1,{},PHASE_BALANCED_POLICY,pinned=True)[1]
    assert blocked['state']=='blocked'
    tighter={**PHASE_BALANCED_POLICY,'output_tokens':1100}
    assert 'output_estimate' in f.group_usage(packet,tasks,'candidate',limit,{},tighter)['blocking_reasons']

def test_single_attempt_error_never_calls_correction(tmp_path,monkeypatch):
    packet,tasks=grouping_fixture(1)
    size=f.group_usage(packet,tasks,'candidate',100000,{},PHASE_BALANCED_POLICY)['input_codepoints']
    config={'slice_max_input_codepoints':size+1,'semantic_budget_policy':budget_policy_pin()}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'batch_id':'isolated'},
        knowledge=SimpleNamespace(_batch=lambda _: {'slice_config':config})))
    monkeypatch.setattr(f,'read_pass_input',lambda *a,**k: {'packet':packet})
    monkeypatch.setattr(f,'pass_packet',lambda *a:packet)
    monkeypatch.setattr(f,'image_catalog',lambda *a:{})
    monkeypatch.setattr(f,'persist_group_plan',lambda *a:None)
    calls=[]
    def rejected(*args,**kwargs):
        calls.append(kwargs['attempt_limit']);assert args[6] is None
        raise ContractError('INVALID_SCHEMA','$','isolated invalid response')
    monkeypatch.setattr(f,'model_call',rejected)
    with pytest.raises(ContractError,match='no correction call'):
        f.run_pass(adapter,{'workflow_id':'isolated'},object())
    assert calls==[1]

def test_recovery_only_accepts_structured_reserve_only_failure():
    usage={'input_codepoints':29459,'correction_reserve_codepoints':2048,
           'estimated_output_tokens':4656,'blocking_reasons':['input_with_correction_reserve']}
    def report(value):
        return {'error_code':'READING_WINDOW_OVERSIZE','reason':'READING_WINDOW_OVERSIZE at $: '+json.dumps(value)+'; one whole task does not fit; no truncation or model call'}
    assert mechanical_failure_codes(report(usage),'correction-reserve-admission/v1')==['READING_WINDOW_OVERSIZE']
    for change in ({'input_codepoints':30001},{'estimated_output_tokens':8000},{'blocking_reasons':['whole_image_budget']}, {'input_codepoints':True}):
        with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED'):
            mechanical_failure_codes(report({**usage,**change}),'correction-reserve-admission/v1')
    with pytest.raises(ContractError,match='RECOVERY_NOT_ALLOWED'):
        mechanical_failure_codes(report(usage),'correction-reserve-admission/v1',input_limit=25000)
