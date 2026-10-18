import copy,json
import pytest
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.validation import fingerprint
from test_global_plan_inputs import fixture_value
from test_reduce_execution_recovery import stopped_reducer
from test_p3_knowledge_build import vault
import fixed_global_packets as g
from reduce_target_recovery import contract_hash


def page(ref,key='shared',path=None):
 return {'candidate_refs':[ref],'identity':{'kind':'entity','identity_key':key,'canonical_name':'Shared topic','aliases':[]},'path':path or '30_Cards/'+key+'.md'}


def response(pages):return {'pages':pages,'omitted_candidate_refs':[],'reason':'Provisional scope-preserving routing'}


def project(*args):return {'input_size':len(json.dumps(args[1],ensure_ascii=False))+100}


def proposal_response(pages,queries=None):
 return {'pages':pages,'detail_unit_refs':queries or [],'reason':'u2 needs a version-specific split; retain p1'}


def proposal_page(refs,key='shared'):
 p=page('unused',key);p.pop('candidate_refs');p['proposal_refs']=refs;return p


def test_proposal_members_are_expanded_by_program_with_exact_coverage():
 v=fixture_value();v['view']['proposals'][0]['candidate_refs'].append('c2');v['view']['proposals'].pop()
 before=copy.deepcopy(v);s=g.initial();packet=g.next_packet(v,g.units(v),s,project)
 assert 'candidates' not in packet['view']
 result,pending=g.accept_proposals(v,s,packet,proposal_response([proposal_page(['p1'])]))
 assert not pending and g.complete(v,result)['pages'][0]['candidate_refs']==['c1','c2']
 assert v==before


def test_detail_is_requested_only_for_one_unit_and_receives_its_reason():
 v=fixture_value();s=g.initial();packet=g.next_packet(v,g.units(v),s,project)
 s,pending=g.accept_proposals(v,s,packet,proposal_response([proposal_page(['p1'],'v1')],['u2']))
 assert len(pending)==1 and pending[0]['unit']=='u2'
 local=g.detail_packet(v,pending[0],s,project)
 assert [r[0] for r in local['view']['candidates']]==['c2']
 assert 'version-specific split' in local['view']['coordination']['detail_reason']
 assert local['previous_state_sha256']==g.state_digest(s)
 s=g.accept(v,s,local,response([page('c2','v2')]))
 assert {r for p in g.complete(v,s)['pages'] for r in p['candidate_refs']}=={'c1','c2'}


@pytest.mark.parametrize('bad',['foreign_query','double_query','assigned_and_query','missing','foreign_proposal','candidate_handle'])
def test_proposal_routing_cannot_lose_or_invent_members(bad):
 v=fixture_value();s=g.initial();packet=g.next_packet(v,g.units(v),s,project)
 d=proposal_response([proposal_page(['p1','p2'])])
 if bad=='foreign_query':d['detail_unit_refs']=['u99']
 if bad=='double_query':d['detail_unit_refs']=['u2','u2']
 if bad=='assigned_and_query':d['detail_unit_refs']=['u2']
 if bad=='missing':d['pages'][0]['proposal_refs']=['p1']
 if bad=='foreign_proposal':d['pages'][0]['proposal_refs']=['p1','p99']
 if bad=='candidate_handle':d['pages'][0]['proposal_refs']=['c1','c2']
 with pytest.raises(ContractError):g.accept_proposals(v,s,packet,d)
 assert s==g.initial()


def test_output_slots_bound_light_input_without_splitting_connected_unit(monkeypatch):
 v=fixture_value();monkeypatch.setattr(g,'PROPOSAL_OUTPUT_SLOTS',1)
 packet=g.next_packet(v,g.units(v),g.initial(),lambda *_:{'input_size':1})
 assert packet['proposal_refs']==['p1']
 v['view']['proposals'][1]['candidate_refs'].append('c1')
 with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
  g.next_packet(v,g.units(v),g.initial(),lambda *_:{'input_size':1})


def test_full_coverage_stable_budget_and_indivisible_oversize(monkeypatch):
 v=fixture_value();
 for proposal in v['view']['proposals']:proposal['identity_hints']=[page('c1')['identity']]
 us=g.units(v);s=g.initial();one=g.packet_view(v,us[:1],s);two=g.packet_view(v,us,s)
 monkeypatch.setattr(g,'LIMIT',project('x',one)['input_size'])
 packet=g.next_packet(v,us,s,project)
 assert packet['unit_count']==1 and packet['candidate_refs']==['c1']
 assert packet['input_codepoints']<=g.LIMIT and project('x',two)['input_size']>g.LIMIT
 s=g.accept(v,s,packet,response([page('c1')]))
 # Append is offered by name, with previous scope and version preserved exactly.
 packet2=g.next_packet(v,us[1:],s,lambda *_:{'input_size':100})
 assert packet2['view']['coordination']['planned_pages'][0]['topics']==['Source group 1']
 assert 'candidate_boundaries' not in packet2['view']['coordination']['planned_pages'][0]
 s=g.accept(v,s,packet2,response([page('c2')]))
 assert g.complete(v,s)['pages'][0]['candidate_refs']==['c1','c2']
 monkeypatch.setattr(g,'LIMIT',1)
 with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):g.next_packet(v,us,g.initial(),project)


@pytest.mark.parametrize('bad',['coverage','duplicate','path','state','unoffered'])
def test_packet_conflicts_never_silently_merge_or_lose_candidates(bad):
 v=fixture_value();us=g.units(v);s=g.initial();p=g.next_packet(v,us[:1],s,project);s=g.accept(v,s,p,response([page('c1')]))
 p=g.next_packet(v,us[1:],s,project);d=response([page('c2')])
 if bad=='coverage':d['pages']=[]
 if bad=='duplicate':d['omitted_candidate_refs']=['c2']
 if bad=='path':d['pages'][0]['path']='30_Cards/changed.md'
 if bad=='state':s['reasons'].append('changed')
 if bad=='unoffered':p['view']['coordination']['planned_pages']=[]
 with pytest.raises(ContractError):g.accept(v,s,p,d)


def test_shared_candidates_keep_proposals_in_same_unit_and_versions_may_stay_separate():
 v=fixture_value();v['view']['proposals'].append({**copy.deepcopy(v['view']['proposals'][1]),'proposal':'p3','candidate_refs':['c1','c2']})
 assert len(g.units(v))==1
 p=g.next_packet(v,g.units(v),g.initial(),project)
 s=g.accept(v,g.initial(),p,response([page('c1','v1'),page('c2','v2')]))
 assert len(g.complete(v,s)['pages'])==2
 assert v['view']['candidates'][1]['applicability']=='Source version 2'


@pytest.mark.parametrize('bad',['valid','unresolved','own_semantic'])
def test_derived_hold_recovery_checks_parent_evidence_not_accepted_label(vault,monkeypatch,bad):
 a,w,card,req,ref=stopped_reducer(vault,monkeypatch,kind='global-reduce')
 from ingest_kanban import desired_graph
 nodes={n.name:n for n in desired_graph(a.workflow,w)};parent=nodes[card['node']].parents[0]
 failure=json.loads((vault/ref).read_text());failure.update(error_code='DEPENDENCY_EXECUTION_BLOCKED',reason=parent)
 if bad=='own_semantic':failure.update(error_code='SEMANTIC_REVIEW_REQUIRED',reason='own failed result')
 if bad=='unresolved':
  import ingest_kanban
  original=ingest_kanban.domain_completed
  monkeypatch.setattr(ingest_kanban,'domain_completed',lambda service,value,node:False if node.name==parent else original(service,value,node))
 (vault/ref).write_text(json.dumps(failure))
 import hashlib
 req.update(recovery_scope='dependency-resolved/v1',target_contract_sha256=contract_hash())
 req['entries'][0]['failure_sha256']=hashlib.sha256((vault/ref).read_bytes()).hexdigest();req['input_digest']=mutation_digest(req)
 before={p:p.read_bytes() for p in vault.rglob('*.json')}
 if bad!='valid':
  with pytest.raises(ContractError):a.recover_reduce_execution(req)
  assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
 else:
  assert a.recover_reduce_execution(req,dry_run=True)['ok']
  result=a.recover_reduce_execution(req)
  saved=json.loads((vault/result['report_ref']).read_text())
  assert saved['entries'][0]['derived_failure_only'] and not saved['entries'][0]['parent_work_rerun']


@pytest.mark.parametrize('mode',['empty','missing','tampered'])
def test_empty_result_is_audited_but_not_promoted_to_evidence(monkeypatch,mode):
 from types import SimpleNamespace as NS
 import ingest_kanban,reduce_target_recovery as recovery,fixed_evidence
 record={'pass_kind':'citation','candidates':[] if mode=='empty' else [{'candidate_id':'c'}]}
 record['pass_id']=fingerprint(record)
 if mode=='tampered':record['candidates']=[]
 parent=NS(name='pass-slice:parent',kind='pass-slice');child=NS(parents=('pass-slice:parent',))
 k=NS(_slice=lambda *_:{'task_ids':['task'],'state':'cancelled'},_pass_index=lambda *_:({}, {'task':[record]}),_document_registry_revision=lambda:1)
 svc=NS(knowledge=k,verify_pass_skips=lambda *_:None,skipped_pass_tasks=lambda *_:set())
 monkeypatch.setattr(ingest_kanban,'desired_graph',lambda *_:[parent])
 monkeypatch.setattr(ingest_kanban,'domain_completed',lambda *_:True)
 def check(_k,records,*_):
  if records:raise ContractError('INCOMPLETE_COVERAGE','$','required dependency inspection missing')
 monkeypatch.setattr(fixed_evidence,'original_evidence',check)
 value={'batch_id':'b','actor':'a','pass_revision':{'state':'accepted'}}
 if mode=='empty':
  result=recovery.verify(NS(workflow=svc),value,child,{}, {},{'reason':parent.name},[],'dependency-resolved/v1')
  assert result['parents_evidence'][0]['empty_citations']==[record['pass_id']]
 else:
  with pytest.raises(ContractError):recovery.verify(NS(workflow=svc),value,child,{}, {},{'reason':parent.name},[],'dependency-resolved/v1')


def test_incremental_routing_retains_source_version_paths_without_truncation():
 v=fixture_value()
 for n,p in enumerate(v['view']['proposals'],1):
  p.update(source_resource_id='r'+str(n),source_path='10_Raw/Standard-Version-'+str(n)+'.pdf')
 us=g.units(v);s=g.initial();packet=g.next_packet(v,us[:1],s,project)
 columns=packet['view']['columns']['proposals'];row=dict(zip(columns,packet['view']['proposals'][0]))
 assert packet['view']['sources'][row['source']]==['r1','10_Raw/Standard-Version-1.pdf']
 s=g.accept(v,s,packet,response([page('c1','v1')]))
 # A matching related name must still expose which source version was routed before.
 v['view']['proposals'][1]['identity_hints']=[page('c1','v1')['identity']]
 next_view=g.next_packet(v,us[1:],s,project)['view']
 assert next_view['coordination']['planned_pages'][0]['source_paths']==['10_Raw/Standard-Version-1.pdf']
 source=dict(zip(next_view['columns']['proposals'],next_view['proposals'][0]))['source']
 assert next_view['sources'][source]==['r2','10_Raw/Standard-Version-2.pdf']
