"""Budget freshness and interrupted immutable writes; no domain model calls."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from hermes_source_units import ContractError
from page_budget_store import INDEX,operation,Snapshot
from page_pair_calls import PageCalls,key
from test_page_call_contracts import system,item,reserve,returned,settle

def test_large_payloads_are_absent_from_index_and_scan_reads_once(system):
    b,*_=system;a=item('a');q=b.reserve([a],{'system':'s','user':'evidence '*30000},{'large':'schema'*1000})
    returned(b,q,{'p1':{'content':'native fixture '*10000}});settle(system,q)
    raw=json.loads(b.path.read_text());assert raw['contract']==INDEX and len(b.path.read_bytes())<5000
    with operation() as stats:
        for _ in range(10):
            with b.scan() as scan:
                assert scan.row(key(a[0]))['receipt']['state']=='valid'
                assert scan.request(q)['response']['raw']
        assert list(stats['index_loads'].values())==[1]
        assert stats['payload_loads'] and max(stats['payload_loads'].values())==1

def test_response_file_survives_index_update_crash(system,monkeypatch):
    b,*_=system;a=item('a');q=reserve(b,a);save=b._save
    monkeypatch.setattr(b,'_save',lambda _:(_ for _ in ()).throw(RuntimeError('index interrupted')))
    with pytest.raises(RuntimeError):returned(b,q,{'p1':{'content':'retained raw'}})
    monkeypatch.setattr(b,'_save',save);settle(system,q)
    assert b.status(*a)['receipt']['state']=='valid' and len(b.status(*a)['attempts'])==1

def test_receipt_file_survives_index_crash_without_persisting_valid_page_again(system,monkeypatch):
    b,_,writes,calls,*_=system;a=item('a');q=reserve(b,a);returned(b,q,{'p1':{'content':'retained page'}})
    save=b._save;monkeypatch.setattr(b,'_save',lambda _:(_ for _ in ()).throw(RuntimeError('receipt index interrupted')))
    with pytest.raises(RuntimeError):settle(system,q)
    assert len(calls)==1
    monkeypatch.setattr(b,'_save',save);settle(system,q)
    assert len(calls)==1 and len(writes)==1

def test_concurrency10_fresh_reservations_do_not_lose_budget(system):
    b,*_=system
    def work(i):
        other=PageCalls(b.root,b.guard);return reserve(other,item(str(i)))
    with ThreadPoolExecutor(max_workers=10) as pool:requests=list(pool.map(work,range(10)))
    assert len(set(requests))==len(b.read()['pages'])==10
    assert all(len(r['attempts'])==1 for r in b.read()['pages'].values())

def test_stale_scanning_snapshot_never_grants_a_completed_page(system):
    b,*_=system;a=item('a')
    with b.scan() as old:
        assert old.row(key(a[0])) is None
        q=reserve(b,a);returned(b,q,{'p1':{'content':'done'}});settle(system,q)
        assert old.row(key(a[0])) is None
        with pytest.raises(ContractError,match='valid page'):reserve(b,a)

def test_payload_is_checked_again_in_next_operation(system):
    b,*_=system;a=item('a');q=reserve(b,a)
    with b.scan() as s:assert s.request(q)
    pointer=json.loads(b.path.read_text())['requests'][q]['payload']
    (b.root/pointer['ref']).write_text('{}')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):
        with b.scan() as s:s.request(q)
