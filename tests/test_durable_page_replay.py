"""Isolated native-proof adapter fixtures, never accepted real domain outputs."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from page_pair_calls import PageCalls,key
from fixed_reduce import page_view
from fixed_semantic import prepare_request_projection
from fixed_semantic_schema import schema_for
from semantic_output import check
from durable_page_replay import read_outcome
from test_global_plan_inputs import fixture_value

@pytest.fixture
def saved(tmp_path):
    a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'isolated','node':'global-reduce'};v=fixture_value()
    page={'identity':{'kind':'entity','identity_key':'isolated','canonical_name':'Isolated','aliases':[]},'path':'entities/isolated.md','candidate_refs':['c1']}
    view=page_view(a,v,page);view['page_scope_contract']='assigned-candidates/v1'
    wire_view={'contract':'durable-page-calls/v1','pages':{'p1':{k:x for k,x in view.items() if k!='original_evidence'}},
        'original_evidence':view['original_evidence'],'page_members':{'p1':key(page)}}
    sc=schema_for('page-pair',wire_view);prepared=prepare_request_projection('page-pair',wire_view,sc,[])
    root=tmp_path/'_system/ledgers/ingest-workflows/isolated';book=PageCalls(root,lambda:'initial')
    q=book.reserve([(page,view)],prepared['wire'],sc);raw=json.dumps({'results':{'p1':{'content':'# Isolated fixture\nConditions retained.'}}})
    ref='_system/ledgers/ingest-workflows/isolated/semantic-calls/fixture.json';meta={'origin':'isolated_fixture'}
    _write_atomic(tmp_path/ref,_json_bytes({'phase':'page-pair','page_request_id':q,'model':meta}))
    book.capture(q,raw,ref,meta)
    def persist(k,draft,*args):
        ref=f'_system/ledgers/ingest-workflows/isolated/page-drafts/{fingerprint(view)}.md'
        _write_atomic(tmp_path/ref,draft['content'].encode());return {'draft_ref':ref,'content_sha256':fingerprint(draft['content'])}
    book.settle(q,lambda k,d:check('page-write',None,None,None,d),persist,lambda *_:True)
    prove=lambda p,w,s,i:(raw,{}) if w==prepared['wire'] and s==sc and i==[] else None
    return a,b,v,view,book,q,prove

def test_saved_native_body_replays_without_writes_or_allowance_change(saved):
    a,b,v,view,book,q,prove=saved;before=book.path.read_bytes()
    result=read_outcome(a,b,view,prove,[],value=v)
    assert result['kind']=='valid' and result['draft']['content'].startswith('# Isolated fixture')
    assert read_outcome(a,b,view,prove,[],value=v)==result and book.path.read_bytes()==before
    assert len(next(iter(book.read()['pages'].values()))['attempts'])==1


def test_one_read_only_operation_loads_each_payload_once_but_keeps_authority_live(saved):
    from page_budget_store import operation
    a,b,v,view,book,q,prove=saved
    with operation() as stats:
        assert read_outcome(a,b,view,prove,[],value=v)['kind']=='valid'
        assert read_outcome(a,b,view,prove,[],value=v)['kind']=='valid'
        assert list(stats['index_loads'].values())==[1]
        assert max(stats['payload_loads'].values())==1
        assert max(stats['file_loads'].values())==1
        with pytest.raises(ContractError):read_outcome(a,b,view,lambda *_:None,[],value=v)

@pytest.mark.parametrize('damage',['current_input','receipt','raw','members','native_proof','unknown'])
def test_changed_or_unknown_native_page_proof_cannot_be_reused(saved,damage):
    a,b,v,view,book,q,prove=saved;ledger=book.read()
    if damage=='current_input':v=copy.deepcopy(v);v['view']['candidates'][0]['finding']='changed source claim'
    if damage=='receipt':next(iter(ledger['pages'].values()))['receipt']['result']['content']='changed'
    if damage=='raw':ledger['requests'][q]['response']['raw']='changed'
    if damage=='members':ledger['requests'][q]['members']['p1']['page_key']='other'
    if damage=='unknown':
        ledger['requests'][q].pop('response')
        response=book.root/'page-call-payloads/responses'/f'{q}.json'
        if response.exists():response.unlink()
    if damage=='native_proof':prove=lambda *_:None
    book.save(ledger);before=book.path.read_bytes()
    with pytest.raises((ContractError,KeyError)):read_outcome(a,b,view,prove,[],value=v)
    assert book.path.read_bytes()==before
