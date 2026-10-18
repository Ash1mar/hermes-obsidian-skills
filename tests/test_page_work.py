"""Bounded page work uses ordinary finite journals, never isolated domain acceptance."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
import fixed_semantic as fixed
import fixed_reduce
import fixed_global_packets as g
import fixed_page_work as work
from test_global_plan_inputs import fixture_value
from test_incremental_global_reduce import page,response,proposal_page,proposal_response


def setup(tmp_path,monkeypatch,value):
 b={'workflow_id':'fixture','node':'global-reduce','task_id':'native'}
 a=NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]}),
      worker_check=lambda *_:None,worker_complete=lambda *_:{'ok':True})
 monkeypatch.setattr(fixed,'canonical_binding',lambda *_a,**_k:(b,{}))
 monkeypatch.setattr(fixed_reduce,'prepare',lambda *_:('fixture-input',value))
 monkeypatch.setattr(fixed,'attach_receipt',lambda *_:None)
 submitted=[];monkeypatch.setattr(fixed_reduce,'submit',lambda *args:submitted.append(copy.deepcopy(args[-1])) or {'ok':True})
 return a,b,submitted


def test_oversized_page_uses_existing_detail_then_native_bodies_with_complete_originals(tmp_path,monkeypatch):
 v=fixture_value();v['view']['original_evidence']['texts']={'s1':'A'*14500,'s2':'B'*14500}
 a,b,submitted=setup(tmp_path,monkeypatch,v);phases=[]
 def call(_a,_b,phase,view,*args,**kwargs):
  phases.append(phase)
  if phase=='global-plan':return proposal_response([proposal_page(['p1','p2'])]),'routing'
  if phase=='global-detail':
   assert 'page_budget' in view and len(view['page_budget']['suggested_fit_candidate_groups'])==2
   assert {r[0] for r in view['candidates']}=={'c1','c2'}
   return response([page('c1','version-one'),page('c2','version-two')]),'refined'
  assert phase=='page-write' and len(view['candidates'])==1
  assert len(next(iter(view['original_evidence']['texts'].values())))==14500
  return {'content':'# Native fixture\n'+view['candidates'][0]['candidate']},'body-'+view['candidates'][0]['candidate']
 monkeypatch.setattr(fixed,'model_call',call)
 assert fixed.execute(a,'fixture-binding',lambda:None)['ok']
 assert phases==['global-plan','global-detail','page-write','page-write']
 assert {c for p in submitted[0]['pages'] for c in p['candidate_refs']}=={'c1','c2'}
 drafts=list(tmp_path.rglob('page-drafts/*.md'));assert len(drafts)==2
 assert {p.read_text().splitlines()[-1] for p in drafts}=={'c1','c2'}


def test_one_local_refusal_preserves_the_other_real_markdown_without_formal_submit(tmp_path,monkeypatch):
 v=fixture_value();a,b,submitted=setup(tmp_path,monkeypatch,v);written=[]
 def call(_a,_b,phase,view,*args,**kwargs):
  if phase=='global-plan':return proposal_response([proposal_page(['p1'],'one'),proposal_page(['p2'],'two')]),'routing'
  handle=view['candidates'][0]['candidate'];written.append(handle)
  return ({'blocked_reason':'Original required table is unreadable'} if handle=='c1' else {'content':'# Valid native body\nAll supplied conditions retained'}),'body-'+handle
 monkeypatch.setattr(fixed,'model_call',call)
 with pytest.raises(ContractError,match='1 native bodies, 1 held pages'):fixed.execute(a,'fixture',lambda:None)
 assert written==['c1','c2'] and not submitted
 saved=list(tmp_path.rglob('page-drafts/*.md'));assert len(saved)==1 and 'Valid native body' in saved[0].read_text()
 progress=json.loads(next(tmp_path.rglob('page-work-*.json')).read_text())
 assert len(progress['held'])==1 and len(progress['written'])==1 and 'content' not in progress['written'][0]


def test_failed_later_plan_does_not_discard_independent_ready_page_work(tmp_path,monkeypatch):
 v=fixture_value();a,b,submitted=setup(tmp_path,monkeypatch,v);monkeypatch.setattr(g,'PROPOSAL_OUTPUT_SLOTS',1)
 phases=[]
 def call(_a,_b,phase,view,*args,**kwargs):
  phases.append(phase)
  if phase=='global-plan':
   refs=view['coordination']['proposal_refs']
   return (proposal_response([proposal_page(refs,'one')]) if refs==['p1'] else {'blocked_reason':'Unresolved semantic scope'}),'plan-'+refs[0]
  return {'content':'# Preserved partial native body'},'body'
 monkeypatch.setattr(fixed,'model_call',call)
 with pytest.raises(ContractError,match='Independent page work preserved'):fixed.execute(a,'fixture',lambda:None)
 assert phases==['global-plan','global-plan','page-write'] and not submitted
 assert len(list(tmp_path.rglob('page-drafts/*.md')))==1


def test_changed_evidence_authority_stops_instead_of_becoming_local_deferral(tmp_path,monkeypatch):
 v=fixture_value();a,b,submitted=setup(tmp_path,monkeypatch,v)
 def call(_a,_b,phase,view,*args,**kwargs):
  if phase=='global-plan':return proposal_response([proposal_page(['p1'],'one'),proposal_page(['p2'],'two')]),'plan'
  raise ContractError('SOURCE_CHANGED','$','required original hash differs')
 monkeypatch.setattr(fixed,'model_call',call)
 with pytest.raises(ContractError,match='SOURCE_CHANGED'):fixed.execute(a,'fixture',lambda:None)
 assert not submitted
