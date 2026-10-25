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
