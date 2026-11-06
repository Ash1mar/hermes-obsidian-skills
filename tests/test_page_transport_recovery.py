"""Stopped transport retry retains original per-page reservations and receipts."""
import copy,json
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from test_page_call_contracts import system,item,reserve,returned,settle
from page_transport_recovery import transient

def grant(book,q):
    r=book.read();row=r['requests'][q]
    return {'request_id':q,'members':row['members'],'wire_sha256':fingerprint(row['wire']),
        'schema_sha256':fingerprint(row['schema']),'recovery_id':'isolated',
        'failure_ref':'isolated-failure','failure_sha256':'f'*64,
        'page_reservations':{m['page_key']:{'input_sha256':r['pages'][m['page_key']]['input_sha256'],
            'path':r['pages'][m['page_key']]['path'],'attempts_sha256':fingerprint(r['pages'][m['page_key']]['attempts'])}
            for m in row['members'].values()}}

def test_retry_same_pair_then_settle_two_receipts_without_refreshing_ordinals(system):
    b,state,writes,calls,*_=system;q=reserve(b,item('a'),item('b'));g=grant(b,q)
    before=copy.deepcopy(b.read()['pages']);b.claim_transport_retry(q,g,{'task_id':'new-native'})
    assert b.read()['pages']==before and len(b.read()['requests'])==1
    with pytest.raises(ContractError,match='already dispatched'):b.claim_transport_retry(q,g,{'task_id':'duplicate'})
    returned(b,q,{'p1':{'content':'a'},'p2':{'content':'b'}});settle(system,q)
    r=b.read();assert len(writes)==2 and all(len(p['attempts'])==1 and p['receipt']['state']=='valid' for p in r['pages'].values())
    assert json.loads(b.path.read_text())['requests'][q]['transport_retries'][0]['state']=='returned'
    with pytest.raises(ContractError):b.claim_transport_retry(q,g,{'task_id':'must-not-send'})

@pytest.mark.parametrize('change',['wire','members','page','stop'])
def test_changed_or_stopped_reservations_hold_before_transport_dispatch(system,change):
    b,state,*_=system;q=reserve(b,item('a'));g=grant(b,q)
    if change=='wire':g['wire_sha256']='wrong'
    if change=='members':g['members']={}
    if change=='page':next(iter(g['page_reservations'].values()))['attempts_sha256']='wrong'
    if change=='stop':state['cancelled']=True
    before=b.path.read_bytes()
    with pytest.raises(ContractError):b.claim_transport_retry(q,g,{'task_id':'held'})
    assert b.path.read_bytes()==before

def test_unknown_with_possible_output_or_nontransient_status_never_gets_transport_grant():
    row={'state':'completion_unknown','completed_output':False,'error':{'class':'InternalServerError','status_code':504}}
    assert transient(row)
    assert not transient({**row,'response_evidence':{'partial':['maybe']}})
    assert not transient({**row,'error':{'class':'PermissionDeniedError','status_code':403}})

def test_shared_plan_recognizes_only_the_exact_sealed_page_transport_admission():
    from hermes_source_units.execution_plan import authenticated_page_transport
    row={'request_id':'a'*64,'wire_sha256':'b'*64,'schema_sha256':'c'*64,'failure_sha256':'d'*64,
        'failure_ref':'failure.json','session_id':'native','members':{'p1':{'page_key':'page'}},'page_reservations':{'page':{}}}
    selected={'node':'global-reduce','task_id':'ended','failure_sha256':'e'*64,'page_transport_retry':True}
    request={'entries':[selected]};entry={'node':'global-reduce','previous_task_id':'ended','failure_sha256':'e'*64,
        'failure':{'error_code':'NATIVE_COMPLETION_UNKNOWN'},'ended_run_ids':['122'],
        'page_transport_retry':{'contract':'durable-page-transport-recovery/v1','source_identity':'f'*64,
            'new_semantic_ordinals':0,'original_unknowns_preserved':True,'requests':[row]}}
    assert authenticated_page_transport(request,entry)
    for name,value in [('new_semantic_ordinals',1),('original_unknowns_preserved',False),('source_identity','changed')]:
        bad=copy.deepcopy(entry);bad['page_transport_retry'][name]=value
        assert not authenticated_page_transport(request,bad)
    bad=copy.deepcopy(request);bad['entries'][0]['task_id']='other'
    assert not authenticated_page_transport(bad,entry)
