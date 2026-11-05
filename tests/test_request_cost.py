import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
import request_cost as cost

def test_complete_schema_instruction_counted_and_unknown_is_not_zero():
    a=cost.measure({'system':'decode','user':'条件，例外，反证'}, {'type':'object'},encoding='')
    b=cost.measure({'system':'decode longer','user':'条件，例外，反证'}, {'type':'object','description':'full schema'},encoding='')
    assert b['utf8_bytes']>a['utf8_bytes'] and a['text_tokens'] is None and a['billable_tokens'] is None

def test_character_order_cannot_override_measured_token_order(monkeypatch):
    class Encoder:
        def encode(self,text,**kw):return list(range(100 if 'short' in text else 10))
    monkeypatch.setattr(cost,'tokenizer',lambda _:Encoder())
    a={'input_size':5,'request_cost':cost.measure({'user':'short'}, {},encoding='fixture')}
    b={'input_size':50,'request_cost':cost.measure({'user':'longer'}, {},encoding='fixture')}
    assert cost.rank(b)<cost.rank(a)

def test_detail_reason_separates_budget_ambiguity_and_correction():
    assert cost.detail_reason('global-detail',{'page_budget':{}})=='page_input_budget'
    assert cost.detail_reason('global-detail',{'coordination':{'detail_reason':'split'}})=='grouping_ambiguity'
    assert cost.detail_reason('global-detail',{}, {'code':'INVALID_SCHEMA'})=='semantic_correction'
