"""Earlier cost hints never author semantic groups or change historical wires."""
import copy
from test_global_plan_inputs import fixture_value
from fixed_semantic import prepare_request_projection
from input_budget import profile
import fixed_global_packets as packets
import page_planning_budget as budget

def test_early_proposal_costs_use_pinned_final_body_construction():
    value=fixture_value();value['view'].update(planning_budget_contract=budget.CONTRACT,
        early_page_budget_contract='proposal-final-body-cost/v1',body_call_contract='independent-page-body/v1',
        page_execution_contract='unified-page-execution/v3')
    value['backend']['input_budget']=profile('pages-40000');before=copy.deepcopy(value)
    with packets.planning_scope(value):view=packets.packet_view(value,packets.units(value),packets.initial(),project=prepare_request_projection)
    h=view['page_budget_hints'];assert h['limit_codepoints']==40000 and h['output_budget']['output_tokens']==8192
    assert [r[0] for r in h['proposal_body_costs']['rows']]==['p1','p2']
    assert {c for r in h['proposal_body_costs']['rows'] for c in r[1]}=={'c1','c2'}
    assert value==before and 'not yet chosen' in h['parent_and_final_identity']

def test_only_future_contract_gains_new_hint_fields():
    v=fixture_value();v['view']['planning_budget_contract']=budget.CONTRACT
    h=packets.packet_view(v,packets.units(v),packets.initial())['page_budget_hints']
    assert 'proposal_body_costs' not in h
