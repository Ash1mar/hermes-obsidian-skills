"""A certified successor supersedes only its own immutable old source replay."""
import copy
from types import SimpleNamespace
import pytest
from hermes_source_units import ContractError
from test_citation_amendment_safety import view
import citation_amendment_compat as compat

@pytest.mark.parametrize('changed',['none','other_task','original','membership','other_node','citation_phase'])
def test_source_rebind_retains_all_unrelated_authority(tmp_path,monkeypatch,changed):
    selection={'task_id':'selected','pass_id':'old','candidate_ids':['one']}
    successor={'pass_id':'new','sequence':2}
    value={'phase':'downstream','operation_id':'amend','plan_sha256':'p','successor':successor,
           'downstream_nodes':['resource-reduce:r']}
    frozen={'selection':selection,'retired_source_ids':['retired'],
        'sources':[{'resource_id':'r','reduction_id':'retired',
            'proposals':[{'candidate_refs':[{'pass_id':'old','candidate_id':'one'},{'pass_id':'unrelated','candidate_id':'two'}]}],
            'omitted_candidate_refs':[]}]}
    packet=view()['original_evidence']
    previous={'view':{'original_evidence':packet},'backend':{'tasks':[{'task_id':'selected'},{'task_id':'untouched'}],
             'task_hashes':['old-selected','same-other']}}
    snapshot=copy.deepcopy(previous)
    snapshot['backend'].update(resource_id='r',mapping={'c1':[{'pass_id':'new','candidate_id':'one'}],
        'c2':[{'pass_id':'unrelated','candidate_id':'two'}]},task_hashes=['new-selected','same-other'])
    binding={'node':'resource-reduce:r'}
    if changed=='other_task':snapshot['backend']['task_hashes'][1]='changed'
    if changed=='original':snapshot['view']['original_evidence']['texts']['x1']='changed'
    if changed=='membership':snapshot['backend']['mapping']['c2'][0]['candidate_id']='changed'
    if changed=='other_node':binding['node']='resource-reduce:other'
    if changed=='citation_phase':value['phase']='citation'
    monkeypatch.setattr(compat,'state',lambda *_:value);monkeypatch.setattr(compat,'plan',lambda *_:frozen)
    monkeypatch.setattr('source_hold_recovery.original',lambda *_:previous)
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,knowledge=SimpleNamespace(
        _pass_index=lambda _:({}, {'selected':[successor]}))))
    w={'workflow_id':'w','pass_revision':{'state':'accepted'}}
    entry={'snapshot_ref':'old.json','snapshot_sha256':'hash'}
    if changed in ('other_task','original','membership'):
        with pytest.raises(ContractError):compat.source_rebind(adapter,w,binding,snapshot,entry)
    else:
        assert compat.source_rebind(adapter,w,binding,snapshot,entry)==(changed=='none')
        assert ('superseded_source_replay' in snapshot['backend'])==(changed=='none')

@pytest.mark.parametrize('change',['none','extra_quota','no_ended_run','wrong_operation','wrong_native','unknown_lost','no_cause','semantic'])
def test_local_recovery_readiness_recognizes_only_its_original_sealed_authority(change):
    from hermes_source_units.execution_plan import authenticated_local_amendment_recovery
    entry={'node':'resource-reduce:r','previous_task_id':'native','ended_run_ids':['1'],'failure_sha256':'a'*64,
       'failure':{'error_code':'RECOVERY_NOT_ALLOWED'},'local_amendment_execution':{
         'operation_id':'local','current_input_sha256':'b'*64,'successor_pass_id':'c'*64,
         'same_lineage_limits':{'citation':1,'source':1,'plan':1,'body':2},'original_reservation_count':1,
         'new_model_calls_granted':0,'unknowns_preserved':True}}
    request={'amendment_id':'local','recovery_scope':'reduce-cancel/v1','target_contract_sha256':'d'*64,
        'entries':[{'node':entry['node'],'task_id':'native','failure_sha256':'a'*64}],
        'basis':[{'ref':'repair.json','sha256':'e'*64}]}
    if change=='extra_quota':entry['local_amendment_execution']['new_model_calls_granted']=1
    if change=='no_ended_run':entry['ended_run_ids']=[]
    if change=='wrong_operation':request['amendment_id']='foreign'
    if change=='wrong_native':request['entries'][0]['task_id']='foreign'
    if change=='unknown_lost':entry['local_amendment_execution']['unknowns_preserved']=False
    if change=='no_cause':request['basis']=[]
    if change=='semantic':entry['failure']['error_code']='SEMANTIC_REVIEW_REQUIRED'
    assert bool(authenticated_local_amendment_recovery(request,entry))==(change=='none')
