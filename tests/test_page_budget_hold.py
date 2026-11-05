"""Complete-input isolation never turns an uncalled oversized page into success."""
import copy
from types import SimpleNamespace as NS
from test_global_plan_inputs import fixture_value
from page_budget_hold import proof,verified

def test_oversize_proof_is_current_complete_and_cannot_be_forged(tmp_path):
    v=fixture_value();v['view']['original_evidence']['texts']['s1']='Complete context AND exceptions. '*2500
    page={'identity':{'kind':'entity','identity_key':'big','canonical_name':'Big','aliases':[]},'path':'big.md','candidate_refs':['c1']}
    a=NS(workflow=NS(vault=tmp_path));p=proof(a,v,page)
    assert p['minimum_complete_input_codepoints']>30000 and p['new_calls_granted']==0
    h={'code':'READING_WINDOW_OVERSIZE','origin':'complete-slot-budget/v1','budget_proof':p}
    assert verified(a,v,page,h)
    changed=copy.deepcopy(h);changed['budget_proof']['input_sha256']='changed'
    assert not verified(a,v,page,changed)
    v['view']['original_evidence']['texts']['s1']='Small complete context'
    assert not verified(a,v,page,h)
