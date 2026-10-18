"""Short routing targets preserve exact identities, old journals and coverage."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from fixed_semantic import prepare_request_projection
from fixed_semantic_schema import schema,schema_for
from test_global_plan_inputs import fixture_value
from test_incremental_global_reduce import page,response,proposal_response
import fixed_global_packets as g
import fixed_global_targets as targets
import semantic_output as output


def fixture(tmp_path):
    v=fixture_value();identity=page('c1')['identity']
    v['view']['proposals'][1]['identity_hints']=[identity]
    units=g.units(v);state=g.initial();first=g.next_packet(v,units[:1],state,prepare_request_projection)
    state=g.accept(v,state,first,response([page('c1')]))
    packet=g.next_packet(v,units[1:],state,prepare_request_projection)
    a=NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]}))
    b={'node':'global-reduce','workflow_id':'fixture','task_id':'native'}
    return v,state,packet,a,b


def test_short_reference_expands_exact_target_and_keeps_candidate_coverage(tmp_path):
    v,s,p,a,b=fixture(tmp_path);before=copy.deepcopy(s)
    short=targets.select(a,b,'global-plan',p,prepare_request_projection)
    target=short['view']['coordination']['planned_pages'][0]
    result,pending=g.accept_proposals(v,s,short,proposal_response([{'target_ref':target['target_ref'],'proposal_refs':['p2']}]))
    assert not pending and result['pages'][0]['candidate_refs']==['c1','c2']
    assert result['pages'][0]['identity']==before['pages'][0]['identity']
    assert result['pages'][0]['path']==before['pages'][0]['path'] and s==before


@pytest.mark.parametrize('bad',['foreign','mixed','missing'])
def test_short_targets_do_not_expand_authority_or_hide_coverage(tmp_path,bad):
    v,s,p,a,b=fixture(tmp_path);p=targets.select(a,b,'global-plan',p,prepare_request_projection)
    row={'target_ref':'t1','proposal_refs':['p2']}
    if bad=='foreign':row['target_ref']='t999'
    if bad=='mixed':row['identity']=page('c2')['identity']
    if bad=='missing':row['proposal_refs']=['p99']
    with pytest.raises(ContractError):g.accept_proposals(v,s,p,proposal_response([row]))


def test_old_consumed_request_keeps_schema_and_attempt_bytes(tmp_path):
    v,s,p,a,b=fixture(tmp_path);path=output.path_for(a,b,'global-plan',p['view']);path.parent.mkdir(parents=True)
    record={'attempts':[{'attempt':1,'state':'rejected','correctable':True}], 'schema_sha256':fingerprint(schema('global-plan'))}
    path.write_text(json.dumps(record));before=path.read_bytes()
    selected=targets.select(a,b,'global-plan',p,prepare_request_projection)
    assert selected is p and schema_for('global-plan',selected['view'])==schema('global-plan')
    assert path.read_bytes()==before
    short=targets.target_view(p['view']);other=output.path_for(a,b,'global-plan',short)
    other.write_text(json.dumps({'attempts':[{'attempt':1}]}))
    with pytest.raises(ContractError,match='allowance reset'):targets.select(a,b,'global-plan',p,prepare_request_projection)


def test_operation_lookup_preserves_old_wire_and_does_not_survive_scope(tmp_path):
    v,s,p,a,b=fixture(tmp_path);units=g.units(v)
    expected=g.next_packet(v,units[1:],s,prepare_request_projection)
    with g.planning_scope(v):
        actual=g.next_packet(v,units[1:],s,prepare_request_projection)
        assert actual==expected
    v['view']['proposals'][0]['summary']='Changed current source grouping'
    assert g.next_packet(v,units[1:],s,prepare_request_projection)['view']!=expected['view']
