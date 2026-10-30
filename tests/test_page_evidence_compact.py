"""Lossless full-request compression; no removal, history reset or fabricated proof."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from fixed_page_evidence import present_compact,restore_compact,COMPACT_CONTRACT

def test_compact_round_trip_preserves_absence_null_conditions_and_supports():
    evidence=[{'source':'r','span':{'start':0,'end':10},'condition':'only when supported'}]*4
    view={'evidence_encoding':COMPACT_CONTRACT,'identity':{'name':'page'},
        'citation_evidence':[{'finding':str(i),'exceptions':None,'evidence_qa':['QA'],
            'source_evidence':evidence,**({'extra':None} if i%2 else {})} for i in range(8)],
        'original_evidence':{'tasks':[{'materials':[{'text':'unchanged','required':True},{'text':'second','required':False,'asset':None}]}]}}
    original=copy.deepcopy(view);compact=present_compact(view)
    assert restore_compact(compact)==original and view==original

def test_complete_unchanged_call_history_is_never_reencoded(tmp_path,monkeypatch):
    import fixed_page_evidence as module
    import fixed_global_targets,semantic_output
    monkeypatch.setattr(fixed_global_targets,'has_legacy_history',lambda *a:True)
    monkeypatch.setattr(semantic_output,'path_for',lambda *a:tmp_path/'absent')
    view={'content':'old wire','citation_evidence':[]}
    assert module.select(None,None,'page-review',view,lambda *a:None)==view
@pytest.mark.parametrize('history_state',['valid','rejected','reserved'])
def test_scoped_history_is_resolved_before_choosing_a_new_encoding(tmp_path,history_state):
    from types import SimpleNamespace as NS
    from semantic_output import path_for
    from fixed_page_evidence import select_review,review_scope,CONTRACT
    a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'fixture','node':'build-finalize'}
    v={'path':'page.md','citation_evidence':[],'original_evidence':{'tasks':[]}}
    assessment={'policy':'policy','risk_reasons':['risk']}
    consumed={**v,'evidence_encoding':CONTRACT,'review_scope':review_scope(assessment)}
    p=path_for(a,b,'page-review',consumed);p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'attempts':[{'state':history_state}]}));before=p.read_bytes()
    def no_pack(*args):raise AssertionError('consumed scoped review must not choose a new wire')
    assert select_review(a,b,v,assessment,no_pack)==consumed
    assert p.read_bytes()==before and len(list(p.parent.glob('*.json')))==1

def test_multiple_consumed_review_presentations_never_grant_a_call(tmp_path):
    from types import SimpleNamespace as NS
    from semantic_output import path_for
    from fixed_page_evidence import select_review,review_scope,review_variants,CONTRACT,COMPACT_CONTRACT
    from hermes_source_units import ContractError
    a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'fixture','node':'build-finalize'}
    v={'path':'page.md'};assessment={'policy':'policy','risk_reasons':['risk']}
    for marker in (CONTRACT,COMPACT_CONTRACT):
        p=path_for(a,b,'page-review',{**v,'evidence_encoding':marker,'review_scope':review_scope(assessment)})
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
    with pytest.raises(ContractError,match='multiple consumed'):select_review(a,b,v,assessment,None)
    variants=review_variants({**v,'evidence_encoding':COMPACT_CONTRACT},assessment)
    assert len(variants)==4 and len(list(p.parent.glob('*.json')))==2
