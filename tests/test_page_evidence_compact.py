"""Lossless full-request compression; no removal, history reset or fabricated proof."""
import copy
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
