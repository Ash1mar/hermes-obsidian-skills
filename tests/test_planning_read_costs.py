"""Same-input mechanical costs and bounded read sessions, no native model calls."""
import copy,json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json
from hermes_source_units.release_reads import operation,body,calculation
from test_global_plan_inputs import fixture_value
from fixed_semantic import prepare_request_projection
import page_planning_budget as budget

def test_shared_reading_is_measured_once_not_added_per_candidate():
    v=fixture_value();c1=v['view']['candidates'][0]
    v['view']['candidates'][1]['source_evidence']=copy.deepcopy(c1['source_evidence'])
    reading=c1['source_evidence'][0]['reading']
    v['view']['original_evidence']['texts']={k:'unchanged conditions and exceptions '*250 for k in v['view']['original_evidence']['texts']}
    calls=[]
    def project(*args):calls.append(1);return prepare_request_projection(*args)
    with budget.operation(v):
        both=budget.probe(v,['c1','c2'],project);count=len(calls)
        assert budget.probe(v,['c1','c2'],project)==both and len(calls)==count
        one=budget.probe(v,['c1'],project);two=budget.probe(v,['c2'],project)
    assert both['reading_refs']==[reading]
    assert both['complete_request_codepoints']<one['complete_request_codepoints']+two['complete_request_codepoints']
    with budget.operation(v):budget.probe(v,['c1','c2'],project)
    assert len(calls)>count

def test_future_hints_do_not_change_existing_packet_or_candidate_coverage():
    import fixed_global_packets as g
    v=fixture_value();before=copy.deepcopy(v)
    old=g.packet_view(v,g.units(v),g.initial())
    assert 'page_budget_hints' not in old
    v['view']['planning_budget_contract']=budget.CONTRACT
    new=g.packet_view(v,g.units(v),g.initial())
    hints=new.pop('page_budget_hints');assert new==old
    assert {c for row in hints['fit_examples'] for c in row['candidate_refs']}=={'c1','c2'}
    v['view'].pop('planning_budget_contract');assert v==before

def test_delivery_completion_shares_data_and_still_reads_control_fresh(tmp_path,monkeypatch):
    import hermes_source_units.delivery_scope as delivery
    data=tmp_path/'_system/knowledge-builds/run/manifest.json';data.parent.mkdir(parents=True);data.write_text('{"ok":true}')
    control=tmp_path/'_system/ledgers/control.json';control.parent.mkdir(parents=True);control.write_text('{"stop":false}')
    service=NS(vault=tmp_path,_run_path=lambda _:data);observed=[]
    def completed(service,run):
        from hermes_source_units.release_reads import current
        reads=current(data)
        assert _load_json(data)==run
        first=_load_json(control);control.write_text('{"stop":true}')
        assert first=={'stop':False} and _load_json(control)=={'stop':True}
        observed.append(reads);return True
    monkeypatch.setattr(delivery,'completed',completed)
    assert delivery.batch_complete(service,{'run_ids':['run']})
    assert observed[0].stats['reads']==1 and observed[0].stats['confirmation_reads']==1
    assert control not in observed[0].records

def test_mutable_progress_is_never_cached_and_foreign_vault_is_independent(tmp_path):
    a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    p=a/'_system/ledgers/ingest-workflows/w/semantic-inputs/page-work-x.json';p.parent.mkdir(parents=True);p.write_bytes(b'old')
    target=b/'page.md';target.write_bytes(b'old')
    class Service:
        vault=b
        @calculation('foreign')
        def inspect(self):
            body(target);target.write_bytes(b'new')
    with operation(a,'outer') as reads:
        assert body(p)==b'old';p.write_bytes(b'new');assert body(p)==b'new'
        with pytest.raises(ContractError,match='SOURCE_CHANGED'):Service().inspect()
        assert reads.records=={}

def test_parsed_read_copies_cannot_modify_cached_evidence(tmp_path):
    p=tmp_path/'manifest.json';p.write_text('{"pages":[{"conditions":["A OR B"],"exceptions":null}]}')
    with operation(tmp_path,'copy-isolation') as reads:
        first=_load_json(p);first['pages'][0]['conditions'][0]='changed';first['pages'].append({})
        assert _load_json(p)=={'pages':[{'conditions':['A OR B'],'exceptions':None}]}
        assert reads.stats['json_parses']==1 and reads.stats['json_reuses']==1
        reads.confirm()
