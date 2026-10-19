"""Exact consumed boundaries are hints; common native proof remains mandatory."""
import copy,json,sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import fixed_global_packets as g
import fixed_global_replay as replay
import fixed_global_targets as targets
from fixed_semantic import prepare_request_projection
from fixed_semantic_schema import schema_for
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from test_global_plan_inputs import fixture_value
from test_incremental_global_reduce import page,response
from semantic_output import path_for


def recorded(tmp_path):
 v=fixture_value();a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'fixture','node':'global-reduce','task_id':'native','template_hash':'pin'}
 packet=g.next_packet(v,g.units(v),g.initial(),prepare_request_projection)
 packet={**packet,'view':targets.target_view(packet['view'])}
 sc=schema_for('global-plan',packet['view']);wire=prepare_request_projection('global-plan',packet['view'],sc,[])['wire']
 root=tmp_path/'_system/ledgers/ingest-workflows/fixture/semantic-calls';root.mkdir(parents=True)
 audit={'workflow_id':'fixture','node':'global-reduce','phase':'global-plan','validated':True,'template_hash':'pin',
   'schema_sha256':fingerprint(sc),'user_sha256':fingerprint(wire['user']),'model':{'session_id':'fixture'}}
 (root/'call.json').write_text(json.dumps(audit));home=tmp_path/'home';home.mkdir()
 with sqlite3.connect(home/'state.db') as db:
  db.execute('CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT)')
  db.execute('INSERT INTO messages VALUES (?,?,?)',('fixture','user',wire['user']))
 return v,a,b,packet,(root/'call.json').relative_to(tmp_path).as_posix(),home


def test_consumed_boundary_reconstructs_once_without_trial_packing(tmp_path,monkeypatch):
 v,a,b,p,ref,home=recorded(tmp_path);count=[]
 monkeypatch.setattr(g,'next_packet',lambda *a,**k:(_ for _ in ()).throw(AssertionError('greedy repack')))
 def project(*args):count.append(1);return prepare_request_projection(*args)
 actual=replay.recorded_packet(a,b,v,g.units(v),g.initial(),ref,project,home=home)
 assert actual['view']==p['view'] and actual['candidate_refs']==p['candidate_refs'] and len(count)==1
 # This function has no response or completion result: it is not native proof.
 assert set(actual)==set(p)


@pytest.mark.parametrize('change',['source','state','order','schema'])
def test_boundary_reuse_rejects_changed_inputs_and_noncontiguous_units(tmp_path,change):
 v,a,b,p,ref,home=recorded(tmp_path);state=g.initial();remaining=g.units(v)
 if change=='source':v['view']['proposals'][0]['summary']='changed source proposal'
 elif change=='state':state['reasons'].append('changed prefix')
 elif change=='order':remaining.reverse()
 else:
  audit=json.loads((tmp_path/ref).read_text());audit['schema_sha256']='changed';(tmp_path/ref).write_text(json.dumps(audit))
 with pytest.raises(ContractError):replay.recorded_packet(a,b,v,remaining,state,ref,prepare_request_projection,home=home)


def test_section_navigation_is_same_source_and_not_an_automatic_merge(tmp_path):
 v=fixture_value();v['view']['original_evidence']['headings']=[['Standard','10 Maintenance'],['Standard','10 Maintenance']]
 for p in v['view']['proposals']:p['source_resource_id']='same-version'
 state=g.initial();units=g.units(v);first=g.next_packet(v,units[:1],state,prepare_request_projection)
 state=g.accept(v,state,first,response([page('c1','daily')]))
 old=g.packet_view(v,units[1:],state);assert not old['coordination']['planned_pages']
 with g.planning_scope(v):new=g.packet_view(v,units[1:],state,reader_topics=True)
 assert new['page_organization']==g.READER_TOPICS and len(new['coordination']['planned_pages'])==1
 assert len(state['pages'])==1 and state['pages'][0]['candidate_refs']==['c1']
 v['view']['proposals'][1]['source_resource_id']='other-version'
 with g.planning_scope(v):other=g.packet_view(v,units[1:],state,reader_topics=True)
 assert not other['coordination']['planned_pages']


def test_consumed_short_packet_is_not_upgraded_or_given_new_allowance(tmp_path):
 from test_global_target_refs import packing_fixture
 v,s,b,a,project=packing_fixture(tmp_path)
 short=g.next_packet(v,g.units(v),s,project,view_transform=targets.target_view)
 path=path_for(a,b,'global-plan',short['view']);path.parent.mkdir(parents=True);path.write_text('{"attempts":[{"state":"rejected"}]}')
 before=path.read_bytes();chosen=targets.planning_packet(a,b,v,g.units(v),s,project)
 assert chosen==short and 'page_organization' not in chosen['view'] and path.read_bytes()==before
