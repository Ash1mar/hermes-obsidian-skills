"""Automatic repairs depend on complete-wire fit and no consumed work, not titles."""
import copy
from types import SimpleNamespace as NS
import pytest
import finalize_program_repair as module
from hermes_source_units import ContractError
from hermes_source_units.finalize_revision_state import work_id

def fixture(monkeypatch):
    evidence=[{'ref':{'resource_id':'r'*64,'unit_set_id':'u'*64,'unit_id':'i'*64},'condition':'retain this exact necessary condition'}]*5
    view={'page':'e1','path':'30_Cards/any-name.md','identity':{'kind':'entity','canonical_name':'arbitrary'},
        'content':'Supported page content.','existing_page':None,
        'citation_evidence':[{'finding':'fact '+str(i),'exceptions':None,'conditions':['required'],
            'evidence_qa':['unchanged QA'],'source_evidence':evidence} for i in range(45)],
        'original_evidence':{'tasks':[],'texts':{},'qa':{}}}
    item={'state':'deferred','observed_status':'execution_held','diagnostic':{'code':'READING_WINDOW_OVERSIZE'},
        'history':{'attempt_count':0},'issue_id':'issue','page':'e1',
        'logical_work':{'run_id':'run','page_id':'page','revision_id':'revision'},
        'evidence':{'assessment':{'policy':'explicit-page-risks/v1','risk_reasons':['cross_source_merge']}}}
    controls={'works':{}};monkeypatch.setattr(module,'load',lambda *a:controls)
    monkeypatch.setattr('fixed_semantic_media.image_catalog',lambda *a:{})
    monkeypatch.setattr('fixed_semantic_media.select_images',lambda *a:[])
    adapter=NS(workflow=NS(vault=None));w={'workflow_id':'workflow'}
    return adapter,w,{'items':[item]},{'views':[view]},controls

def test_automatic_one_call_repair_only_after_lossless_full_request_fits(monkeypatch):
    a,w,i,s,c=fixture(monkeypatch)
    report=module.candidates(a,w,i,s)
    assert len(report['eligible'])==1
    result=report['eligible'][0]
    assert result['original_input_size']>30000>=result['repaired_complete_input_size']
    assert result['roundtrip_exact'] and result['maximum_new_calls']==1
    # Cosmetic operation or title changes cannot reset a consumed material cycle.
    c['works'][work_id('run','page')]={'grants':[{'calls':{'page-review':{'state':'held'}}}]}
    assert module.candidates(a,w,i,s)['eligible']==[]

@pytest.mark.parametrize('state',['unknown','revision','consumed','loss'])
def test_automatic_repair_never_treats_semantic_failures_or_data_loss_as_fresh_work(monkeypatch,state):
    a,w,i,s,c=fixture(monkeypatch)
    if state=='unknown':i['items'][0]['observed_status']='completion_unknown'
    elif state=='revision':i['items'][0]['observed_status']='revision_required'
    elif state=='consumed':i['items'][0]['history']['attempt_count']=1
    else:
        monkeypatch.setattr('fixed_page_evidence.restore_compact',lambda _: {})
        with pytest.raises(ContractError,match='PROGRAM_REPAIR_INVALID'):module.candidates(a,w,i,s)
        return
    assert not module.candidates(a,w,i,s)['eligible']
