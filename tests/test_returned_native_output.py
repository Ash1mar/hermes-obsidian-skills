"""Native returned-response restoration fixtures; zero model/API requests."""
import copy,json,sqlite3
from pathlib import Path
import pytest
from test_p3_knowledge_build import vault
from test_source_unknown_recovery import fixture
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.validation import fingerprint
import returned_native_output as returned
from fixed_reduce_packets import validate_packet

@pytest.mark.parametrize('change',[None,'answer','request','unknown','sdk_error','snapshot','active'])
def test_admission_and_native_worker_replay_keep_original_ordinal(vault,monkeypatch,tmp_path,change):
 a,w,card,b,value,packet,req,jp,rp,home=fixture(vault,monkeypatch,tmp_path)
 j=json.loads(jp.read_text());attempt=j['attempts'][0];attempt.pop('call_outcome');attempt.update(state='rejected',diagnostic={'code':'INVALID_CANONICAL_VALUE'},correctable=False)
 jp.write_text(json.dumps(j));rp.unlink()
 failure=jp.parent.parent/f"reports/failed-{fingerprint(card['node'])[:16]}.json"
 f=json.loads(failure.read_text());f.update(error_code='INVALID_CANONICAL_VALUE',reason='Local audit metadata is not canonical');failure.write_text(json.dumps(f))
 draft={'proposals':[{'candidate_refs':packet['candidate_refs'],'summary':'Fixture group','identity_hints':[],'path_hints':[]}],'omitted_candidate_refs':[],'reason':'Isolated mechanical fixture'}
 with sqlite3.connect(home/'.hermes/state.db') as db:
  db.execute('INSERT INTO messages VALUES (?,?,?,?)',(attempt['session_id'],'assistant',json.dumps(draft),None))
  if change=='answer':db.execute('DELETE FROM messages WHERE role=?',('assistant',))
  if change=='request':db.execute('UPDATE messages SET content=? WHERE role=?',('different','user'))
 trace=tmp_path/'trace';trace.mkdir();monkeypatch.setattr('hermes_source_units.runtime_telemetry.runtime_directory',lambda *_:trace)
 events=[{'name':name,'workflow_id':w['workflow_id'],'task_id':b['task_id'],'session_id':attempt['session_id'],'phase':'resource-reduce',
          'native_run_id':'1','request_id':'same-request','span_id':name,'error':'TimeoutError' if name=='sdk_model_request' and change=='sdk_error' else None} for name in ('sdk_request_start','sdk_model_request')]
 (trace/'original.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
 req.update(target_contract_sha256=returned.contract_hash());entry=req['entries'][0];entry.pop('source_unknown_isolation');entry['restore_native_output']=True;entry['failure_sha256']=returned.sha(failure)
 if change=='snapshot':entry['snapshot_sha256']='0'*64
 if change=='unknown':j['attempts'][0]['call_outcome']={'state':'completion_unknown'};jp.write_text(json.dumps(j))
 if change=='active':a.kanban.ended.clear()
 req['input_digest']=mutation_digest(req);before=jp.read_bytes()
 if change:
  with pytest.raises(ContractError):a.recover_reduce_execution(req,dry_run=True)
  assert jp.read_bytes()==before;return
 preview=a.recover_reduce_execution(req,dry_run=True);proof=preview['entries'][0]['returned_native_output']
 assert proof['new_model_calls_granted']==0 and proof['unknown_completion_allowed'] is False
 result=a.recover_reduce_execution(req);assert not result['resumed'] and jp.read_bytes()==before
 assert a.recover_reduce_execution(req)['replayed']
 current=a.workflow.status(w['workflow_id']);resume={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':current['revision']};resume['input_digest']=mutation_digest(resume)
 a.resume(resume);current=a.workflow.status(w['workflow_id']);fresh=next(c for c in current['kanban']['task_map'] if c['node']==card['node'])
 binding=json.loads((vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/bindings/{fingerprint(fresh['idempotency_key'])}.json").read_text())
 returned.restore_packet(a,binding,value,packet)
 from semantic_output import execute
 def valid(d):validate_packet(packet,d);return d
 outcome,ref,_=execute(a,binding,'resource-reduce',packet['view'],object(),lambda *_a,**_k:pytest.fail('returned response must never regenerate'),valid,packet=packet)
 assert outcome==draft
 after=json.loads(jp.read_text());assert len(after['attempts'])==1 and after['attempts'][0]['attempt_id']==attempt['attempt_id']
 assert after['attempts'][0]['audit_ref']==ref and after['attempts'][0]['state']=='valid'
 returned.restore_packet(a,binding,value,packet)
