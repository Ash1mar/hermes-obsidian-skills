"""Reuse single admission, retain QA, zero semantic reclassification or asset I/O."""
import copy
import pytest
from hermes_source_units.validation import fingerprint
from input_budget import profile
from page_single_admission import CONTRACT,admit
from page_pair_calls import key
from page_pair_eligibility import pair
from independent_body import prepare,restore,present
from fixed_semantic import prepare_request_projection
from test_independent_body import rows

def admitted():
    items=rows();budget=profile('pages-40000');proofs={}
    for p,v in items:
        v['body_pairing_contract']=CONTRACT
        v['candidates'][0]['evidence_qa'][0].update(qa='needs-qa',qa_note='Uncertainty stays with this page; do not infer the missing formula.')
        q=v['evidence_selection'];q['candidates_sha256']=fingerprint(v['candidates']);q['proof_sha256']=fingerprint({k:x for k,x in q.items() if k!='proof_sha256'})
        size=prepare([(p,v)],prepare_request_projection,budget=budget)[2]['input_size']
        proofs[key(p)]=admit(p,v,size,40000,allowed=True,budget=budget)
    return items,proofs

def test_different_source_and_scope_qa_labels_do_not_force_split_after_single_admission():
    items,proofs=admitted();before=copy.deepcopy(items)
    assert pair(*items,admissions=proofs)['eligible']
    assert not pair(*items)['eligible'] # No detached authority inferred.
    v,_,p,_=prepare(items,prepare_request_projection,budget=profile('pages-40000'))
    assert p['budget_check']['fits'] and restore(present(v))['pages']==v['pages'] and items==before
    assert all(p['semantic_ordinals_granted']==0 and not p['publication_eligibility_granted'] for p in proofs.values())

def test_denied_missing_or_changed_single_page_cannot_borrow_peer_admission():
    items,proofs=admitted();bad=copy.deepcopy(proofs);bad[key(items[0][0])]['allowed']=False
    assert not pair(*items,admissions=bad)['eligible']
    changed=copy.deepcopy(items);changed[0][1]['candidates'][0]['exceptions']=['different exception']
    assert not pair(*changed,admissions=proofs)['eligible']

def test_link_only_material_uses_existing_single_decision_without_image_reads(monkeypatch):
    from pathlib import Path
    items,proofs=admitted()
    for p,v in items:
        v['original_evidence']['tasks'][0]['materials'][0]['linked_assets']=[{'path':'registered.png','sha256':'a'*64}]
        proofs[key(p)]=admit(p,v,1000,40000,allowed=True)
    monkeypatch.setattr(Path,'read_bytes',lambda *_:pytest.fail('pairing must not scan or read asset bytes'))
    assert pair(*items,admissions=proofs)['eligible']
    p,v=items[0];v['original_evidence']['tasks'][0]['materials'][0]['asset']={'path':'required.png','sha256':'b'*64}
    proofs[key(p)]=admit(p,v,1000,40000,allowed=True)
    assert not pair(*items,admissions=proofs)['eligible']

@pytest.mark.parametrize('stop',[False,True])
def test_native_executor_reuses_qa_single_admission_and_preserves_stopped_receipts(tmp_path,monkeypatch,stop):
    import native_page_pairs as native
    from test_page_call_contracts import test_native_pair_bridge_recovers_receipts_without_new_call
    original=native.execute
    def run(*args,**kwargs):
        args=list(args);value=args[2]
        value['view'].update(body_call_contract='independent-page-body/v1',body_pairing_contract=CONTRACT)
        value['backend']['input_budget']=profile('pages-40000');measure=args[8]
        def measure_admitted(*a):
            v,n=measure(*a);v.update(body_call_contract='independent-page-body/v1',body_pairing_contract=CONTRACT)
            v['candidates'][0]['evidence_qa'][0]['qa']='needs-qa'
            q=v['evidence_selection'];q['candidates_sha256']=fingerprint(v['candidates']);q['proof_sha256']=fingerprint({k:x for k,x in q.items() if k!='proof_sha256'})
            return v,n
        args[8]=measure_admitted
        return original(*args,**kwargs)
    monkeypatch.setattr(native,'execute',run)
    test_native_pair_bridge_recovers_receipts_without_new_call(tmp_path,monkeypatch,cancel_after_first=stop,dispatch='unified-page-execution/v3',special=None)

def test_saved_comparison_changes_eligibility_without_changing_budget_or_materials():
    from types import SimpleNamespace as NS
    from saved_body_inputs import compare
    items,_=admitted()
    for _,v in items:v.pop('body_pairing_contract')
    adapter=NS(workflow=NS(knowledge=NS(source=object())))
    before=copy.deepcopy(items)
    old=compare(items,adapter,'pages-40000')
    qa=compare(items,adapter,'pages-40000',pairing_mode='qa_only')
    new=compare(items,adapter,'pages-40000',pairing_mode='single_admission')
    assert old['estimated_requests']==2 and qa['estimated_requests']==new['estimated_requests']==1
    assert old['covered_pages']==qa['covered_pages']==new['covered_pages']==2
    assert items==before and all(r['original_evidence_and_qa_unchanged'] for r in (old,qa,new))
    assert all(r['single_page_preparations']==2 for r in (old,qa,new))
    searched=compare(items,adapter,'pages-40000',pairing_mode='single_admission',partner_search=True)
    assert searched['estimated_requests']==1 and searched['complete_pair_trials']==1

def test_nonadjacent_fit_partners_avoid_two_unnecessary_singles():
    from types import SimpleNamespace as NS
    from saved_body_inputs import compare
    base=admitted()[0][0];items=[]
    for i,n in enumerate((26000,26000,100,100)):
        p,v=copy.deepcopy(base);p['identity']['identity_key']=v['identity']['identity_key']='synthetic-'+str(i)
        p['path']=v['path']=str(i)+'.md';v.pop('body_pairing_contract')
        texts=v['original_evidence']['texts'];texts[next(iter(texts))]=chr(65+i)*n
        q=v['evidence_selection'];q['view_sha256']=fingerprint(v['original_evidence']);q['proof_sha256']=fingerprint({k:x for k,x in q.items() if k!='proof_sha256'})
        items.append((p,v))
    adapter=NS(workflow=NS(knowledge=NS(source=object())))
    adjacent=compare(items,adapter,'pages-40000',pairing_mode='single_admission')
    fit=compare(items,adapter,'pages-40000',pairing_mode='single_admission',partner_search=True)
    assert adjacent['estimated_requests']==3 and fit['estimated_requests']==2
    assert adjacent['covered_pages']==fit['covered_pages']==4 and fit['held_pages']==0
