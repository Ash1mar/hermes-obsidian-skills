"""Fixed profiles, legacy recovery and fully metered split payload reads."""
import copy
import pytest
from hermes_source_units import ContractError
from hermes_source_units.input_budget import profile,validate,limit,check_request
from hermes_source_units.read_accounting import accounting_scope
from page_budget_store import operation,Snapshot
from test_page_call_contracts import system,item,reserve,returned,settle

def test_fixed_profiles_and_historical_values_have_one_phase_limit():
    baseline=profile('baseline-30000');candidate=profile('pages-40000')
    assert limit({},'page-pair')==30000
    assert limit({'backend':{'input_budget':baseline}},'page-pair')==30000
    assert limit({'backend':{'input_budget':candidate}},'page-pair')==40000
    assert candidate['phases']['resource-reduce']['input_codepoints']==30000
    changed=copy.deepcopy(baseline);changed['phases']['page-pair']['input_codepoints']=40000
    with pytest.raises(ContractError):validate(changed)
    assert validate(baseline)==baseline

def test_whole_request_and_output_space_are_separate_from_unknown_actual_usage():
    request={'input_size':35000,'request_cost':{'text_tokens':None,'token_status':'unavailable'}}
    a=check_request(request,profile('baseline-30000'),'page-pair')
    b=check_request(request,profile('pages-40000'),'page-pair')
    assert not a['fits'] and a['split_reasons']==['complete_input_codepoints'] and b['fits']
    assert b['usable_output_tokens']==7168 and b['context_headroom_tokens'] is None and b['billable_input_tokens'] is None

def test_direct_index_request_response_receipt_reads_are_metered_once(system):
    book,*_=system;q=reserve(book,item('a'));returned(book,q,{'p1':{'content':'saved'}});settle(system,q)
    with accounting_scope() as meter,operation() as stats:
        for _ in range(4):assert Snapshot(book.root).full()['requests'][q]['response']
    rows=[r for r in meter.rows.values() if r['origin'].startswith('page_budget.')]
    assert {r['origin'] for r in rows}=={'page_budget.index','page_budget.requests','page_budget.responses','page_budget.receipts'}
    assert all(r['reads']==1 and r['bytes']>0 for r in rows)
    assert sum(r['bytes'] for r in rows)==sum(r['bytes'] for r in stats['io'].values())

def test_failed_operation_still_emits_its_read_cost(system,monkeypatch):
    import hermes_source_units.runtime_telemetry as telemetry
    book,*_=system;reserve(book,item('a'));events=[]
    monkeypatch.setattr(telemetry,'event',lambda name,**row:events.append((name,row)))
    with pytest.raises(RuntimeError):
        with operation():Snapshot(book.root);raise RuntimeError('isolated interruption')
    event=events[-1][1]
    assert event['operation_error']=='RuntimeError' and event['file_io']['index']['reads']==1 and event['includes_failed_operation']

def test_consumed_page_allowance_cannot_change_its_fixed_profile(system):
    from split_page_calls import SplitPageCalls
    book,*_=system;first=SplitPageCalls(book.root,book.guard,budget=profile('baseline-30000'));reserve(first,item('a'))
    second=SplitPageCalls(book.root,book.guard,budget=profile('pages-40000'))
    with pytest.raises(ContractError,match='cannot switch'):reserve(second,item('b'))
