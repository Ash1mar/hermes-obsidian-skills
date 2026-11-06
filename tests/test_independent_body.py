"""Different source/body slots; all responses are isolated fixtures."""
import copy,json
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.input_budget import profile
from fixed_semantic import prepare_request_projection,system_prompt
from fixed_semantic_recovery import _request_text
from independent_body import CONTRACT,prepare,present,restore
from test_unified_page_protocol import views
from test_durable_page_replay import saved

def rows():
    a,b=views()
    for p,v in (a,b):p['identity']=v['identity'];v['body_call_contract']=CONTRACT
    texts=b[1]['original_evidence']['texts'];texts[next(iter(texts))]='Completely different authorized original and exception.'
    return a,b

def test_independent_body_round_trip_keeps_different_qa_scope_and_originals():
    group=rows();v,sc,p,variants=prepare(group,prepare_request_projection,budget=profile('pages-40000'))
    for wire,projection in variants:
        got=json.loads(_request_text(projection['wire']['user']))['input']
        assert restore(got)['pages']==v['pages']
    assert v['pages']['p1']['original_evidence']!=v['pages']['p2']['original_evidence']
    assert 'never use another' in system_prompt('page-pair',v).lower()
    assert all(x['budget_check']['complete_request_codepoints']==x['input_size'] for _,x in variants)

def test_exact_bundle_shared_once_but_context_difference_prevents_sharing():
    a,b=rows();b=(b[0],copy.deepcopy(a[1]));b[1]['identity']['identity_key']='second'
    v=prepare((a,b),prepare_request_projection)[0];p=present(v)
    assert len(p['evidence_pool'])==1 and restore(p)['pages']==v['pages']
    b[1]['original_evidence']['qa']=['Different limitation retained']
    v=prepare((a,b),prepare_request_projection)[0]
    assert len(present(v)['evidence_pool'])==2

def test_no_implicit_three_page_or_image_pair_expansion():
    a,b=rows()
    with pytest.raises(ContractError):prepare((a,b,a),prepare_request_projection)
    with pytest.raises(ContractError):prepare((a,b),prepare_request_projection,images=[{}])

def test_generation_only_complete_cost_includes_first_write_quality_instructions():
    group=rows();old=prepare(group,prepare_request_projection,budget=profile('pages-40000'))[2]
    for _,v in group:v['page_semantics_policy']='generation-only-page-semantics/v1'
    view,_,new,_=prepare(group,prepare_request_projection,budget=profile('pages-40000'))
    assert 'no later independent model review' in new['wire']['system']
    assert new['input_size']>old['input_size']
    assert new['budget_check']['complete_request_codepoints']==new['input_size']

def test_independent_qualification_does_not_require_identical_complete_evidence():
    from page_pair_eligibility import pair
    a,b=rows()
    # Recompute the proof after this fixture's original change; never clear QA.
    p=b[1]['evidence_selection'];p['view_sha256']=fingerprint(b[1]['original_evidence']);p['proof_sha256']=fingerprint({k:v for k,v in p.items() if k!='proof_sha256'})
    assert pair(a,b)['eligible']

def test_read_only_replay_accepts_independent_saved_wire_and_rejects_peer_substitution(saved):
    from fixed_reduce import page_view
    from page_pair_calls import PageCalls
    from durable_page_replay import read_outcome
    from semantic_output import check
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    a,b,value,_,old,_,_=saved
    # New isolated workflow, no allowance migration from the legacy fixture.
    b={**b,'workflow_id':'independent-fixture'};value=copy.deepcopy(value)
    value['view']['body_call_contract']=CONTRACT
    value['view']['page_execution_contract']='unified-page-execution/v3'
    page={'identity':{'kind':'entity','identity_key':'independent','canonical_name':'Independent','aliases':[]},'path':'entities/independent.md','candidate_refs':['c1']}
    view=page_view(a,value,page);view['page_scope_contract']='assigned-candidates/v1'
    wire,sc,p,_=prepare([(page,view)],prepare_request_projection)
    root=a.workflow.vault/f'_system/ledgers/ingest-workflows/{b["workflow_id"]}'
    book=PageCalls(root,lambda:'e');q=book.reserve([(page,view)],p['wire'],sc)
    raw=json.dumps({'results':{'p1':{'content':'# Authorized independent fixture'}}});ref=root.relative_to(a.workflow.vault).as_posix()+'/semantic-calls/fixture.json';meta={'origin':'isolated_fixture'}
    _write_atomic(a.workflow.vault/ref,_json_bytes({'phase':'page-pair','page_request_id':q,'model':meta}))
    book.capture(q,raw,ref,meta)
    def persist(k,draft,*_):
        path=root/'page-drafts'/f'{fingerprint(view)}.md';_write_atomic(path,draft['content'].encode())
        return {'draft_ref':path.relative_to(a.workflow.vault).as_posix(),'content_sha256':fingerprint(draft['content'])}
    book.settle(q,lambda k,d:check('page-write',None,None,None,d),persist,lambda *_:True)
    prove=lambda path,w,s,i:(raw,{}) if w==p['wire'] and s==sc and i==[] else None
    assert read_outcome(a,b,view,prove,[],value=value)['kind']=='valid'
    changed=copy.deepcopy(value);changed['view']['candidates'][0]['finding']='peer evidence is not authorized'
    with pytest.raises(ContractError):read_outcome(a,b,view,prove,[],value=changed)
