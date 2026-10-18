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


def test_actual_model_decoder_accepts_the_short_schema_it_sent(tmp_path):
    import fixed_semantic as fixed
    v,s,p,a,b=fixture(tmp_path);a.worker_check=lambda *_:None
    p=targets.select(a,b,'global-plan',p,prepare_request_projection)
    answer=proposal_response([{'target_ref':'t1','proposal_refs':['p2']}])
    seen=[]
    def caller(phase,view,output_schema,correction):
        seen.append(output_schema)
        return json.dumps(answer),{'origin':'isolated_fixture_not_native_acceptance'}
    result,ref=fixed.model_call(a,b,'global-plan',p['view'],caller,30000,attempt_limit=1)
    assert result==answer and seen==[schema_for('global-plan',p['view'])]
    audit=json.loads((tmp_path/ref).read_text())
    assert audit['schema_sha256']==fingerprint(seen[0])
    fixed.attach_receipt(a,ref,{'ok':True,'validated':True,'state':'fixture'})
    first=(tmp_path/ref).read_bytes()
    fixed.attach_receipt(a,ref,{'ok':True,'validated':True,'state':'fixture'})
    assert (tmp_path/ref).read_bytes()==first


def test_short_diagnostic_excerpts_only_actual_conflicts_without_changing_answer(tmp_path):
    v,s,p,a,b=fixture(tmp_path);p=targets.select(a,b,'global-plan',p,prepare_request_projection)
    bad=proposal_response([{'target_ref':'t1','proposal_refs':['p2']},{'target_ref':'t1','proposal_refs':['p2']}])
    original=copy.deepcopy(bad)
    try:g.accept_proposals(v,s,p,bad)
    except ContractError as exc:diagnostic=output.diagnosis('global-plan',p,bad,exc,state=s)
    assert bad==original and 'failed_response' not in diagnostic
    assert diagnostic['failed_response_sha256']==fingerprint(original)
    assert diagnostic['duplicate_assignments']==['p2']
    assert [r['output_index'] for r in diagnostic['failed_response_excerpt']['pages']]==[0,1]
    assert all(r['page']['target_ref']=='t1' for r in diagnostic['failed_response_excerpt']['pages'])


def packing_fixture(tmp_path):
    v=fixture_value();s=g.initial();b={'workflow_id':'fixture','node':'global-reduce','task_id':'ended'}
    a=NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]}))
    def project(phase,view,sc,images,correction=None):
        actual=prepare_request_projection(phase,view,sc,images,correction)
        if phase!='global-plan':return actual
        short=view.get('output_contract')==targets.CONTRACT
        count=len(view['coordination']['proposal_refs'])
        return {**actual,'input_size':31000 if short and count==2 else 20000 if short else 29000}
    return v,s,b,a,project


def test_new_packets_pack_the_complete_target_wire_and_preserve_the_remainder(tmp_path):
    v,s,b,a,project=packing_fixture(tmp_path);us=g.units(v)
    legacy=g.next_packet(v,us,s,project);assert legacy['unit_count']==2
    packet=targets.planning_packet(a,b,v,us,s,project)
    assert packet['view']['output_contract']==targets.CONTRACT and packet['unit_count']==1
    assert packet['input_codepoints']==20000 and packet['proposal_refs']==['p1']
    result,pending=g.accept_proposals(v,s,packet,proposal_response([{
        **{k:x for k,x in page('c1','v1').items() if k!='candidate_refs'},'proposal_refs':['p1']}]))
    rest=targets.planning_packet(a,b,v,us[packet['unit_count']:],result,project)
    assert not pending and rest['proposal_refs']==['p2'] and rest['candidate_refs']==['c2']


@pytest.mark.parametrize('state',['valid','rejected','reserved'])
def test_consumed_legacy_boundary_cannot_be_repacked_to_reset_calls(tmp_path,state):
    v,s,b,a,project=packing_fixture(tmp_path);packet=g.next_packet(v,g.units(v),s,project)
    path=output.path_for(a,b,'global-plan',packet['view']);path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'attempts':[{'attempt':1,'state':state}]}));before=path.read_bytes()
    selected=targets.planning_packet(a,b,v,g.units(v),s,project)
    assert selected==packet and path.read_bytes()==before


def test_consumed_oversized_short_boundary_is_held_not_repartitioned(tmp_path):
    v,s,b,a,project=packing_fixture(tmp_path);packet=g.next_packet(v,g.units(v),s,project)
    path=output.path_for(a,b,'global-plan',targets.target_view(packet['view']));path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'attempts':[{'attempt':1,'state':'rejected'}]}));before=path.read_bytes()
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        targets.planning_packet(a,b,v,g.units(v),s,project)
    assert path.read_bytes()==before


def test_executor_advances_by_the_measured_packet_without_losing_the_remaining_unit(tmp_path,monkeypatch):
    import fixed_semantic as fixed
    import fixed_reduce
    v,s,b,a,project=packing_fixture(tmp_path);a.worker_check=lambda *_:None;a.worker_complete=lambda *_:{'ok':True}
    monkeypatch.setattr(fixed,'canonical_binding',lambda *_a,**_k:(b,{}))
    monkeypatch.setattr(fixed_reduce,'prepare',lambda *_:('fixture-input',v))
    monkeypatch.setattr(fixed,'prepare_request_projection',project)
    monkeypatch.setattr(fixed,'attach_receipt',lambda *_:None)
    submitted=[];monkeypatch.setattr(fixed_reduce,'submit',lambda *args:submitted.append(copy.deepcopy(args[-1])) or {'ok':True})
    planned=[]
    def call(_a,_b,phase,view,*args,**kwargs):
        if phase=='global-plan':
            refs=view['coordination']['proposal_refs'];planned.append(refs)
            return proposal_response([{**{k:x for k,x in page('unused',ref).items() if k!='candidate_refs'},'proposal_refs':[ref]} for ref in refs]),'fixture-routing-'+refs[0]
        assert phase=='page-write'
        return {'content':'Isolated test body'},'fixture-body'
    monkeypatch.setattr(fixed,'model_call',call)
    assert fixed.execute(a,'fixture-binding',lambda:None)['ok']
    assert planned==[['p1'],['p2']] and {r for p in submitted[0]['pages'] for r in p['candidate_refs']}=={'c1','c2'}
