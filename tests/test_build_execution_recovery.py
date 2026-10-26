"""Isolated Build recovery and finite grants; no production domain execution."""
import copy,hashlib,json,sqlite3
from pathlib import Path
import pytest
from test_build_policy_adoption import held,vault,files
from test_p3_knowledge_build import workflow_request
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint
from ingest_kanban import desired_graph
import build_execution_recovery as recovery
import fixed_review
from fixed_semantic import prepare_request_projection
from fixed_semantic_schema import schema_for
from semantic_output import path_for

@pytest.fixture
def stopped(held,vault,monkeypatch):
    a,w,adopt=held;a.adopt_build_acceptance(adopt);w=a.workflow.status(w['workflow_id'])
    a.workflow.continue_workflow(workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        boundary='checkpoint_1',continue_id='isolated-build',evidence_digest=w['pause_control']['evidence_digest']))
    w=a.workflow.status(w['workflow_id']);w.update(state='build_finalizing',current_stage='build_finalizing')
    a.workflow._write(w)
    node=next(n for n in desired_graph(a.workflow,w) if n.name=='build-finalize')
    key=f'ingest:{w["workflow_id"]}:build-finalize:{node.input_fingerprint}'
    root=vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    binding={'workflow_id':w['workflow_id'],'node':node.name,'actor':w['actor'],'task_id':'old-build',
        'input_fingerprint':node.input_fingerprint,'template_hash':next(p['template_hash'] for p in w['template_pins'] if p['kind']=='build-finalize')}
    bp=root/f'bindings/{fingerprint(key)}.json';bp.parent.mkdir(exist_ok=True);bp.write_bytes(_json_bytes(binding))
    w['kanban']['task_map']=[{'node':node.name,'task_id':'old-build','idempotency_key':key}];a.workflow._write(w)
    row={'id':'old-build','status':'archived','created_by':'ingest-workflow','worker_pid':None,
        'body':json.dumps({'workflow_id':w['workflow_id'],'node':node.name,'vault':str(vault),'executor':'semantic-v1',
            'input_fingerprint':node.input_fingerprint,'worker_binding':str(bp),'template_hash':binding['template_hash']})}
    a.kanban.rows=[row];a.kanban.runs['old-build']=[{'id':1,'started_at':1,'ended_at':2}]
    monkeypatch.setattr(a.kanban,'task_states',lambda *_:{'old-build':'archived'},raising=False)
    monkeypatch.setattr(a.kanban,'task_snapshot',a.kanban.task_snapshot_fresh,raising=False)
    snapshot=fixed_review.read_snapshot(a,binding,{'batch_id':w['batch_id']},True)
    sp=root/f'semantic-inputs/review-{snapshot["input_id"]}.json';sp.parent.mkdir(exist_ok=True);sp.write_bytes(_json_bytes(snapshot))
    view=recovery.view_for(a,binding,snapshot['views'][0],snapshot['backend']['pages']['e1']['assessment'])
    schema=schema_for('page-review',view);wire=prepare_request_projection('page-review',view,schema,[])['wire']
    jp=path_for(a,binding,'page-review',view);jp.parent.mkdir(exist_ok=True)
    attempt={'attempt':1,'attempt_id':fingerprint({'path':jp.name,'attempt':1}),'task_id':'old-build','run_id':'1',
        'session_id':'old-session','state':'rejected','wire_sha256':fingerprint(wire),'correction':None,'correctable':False}
    outcome={'contract':'native-call-outcome/v1','state':'completion_unknown','completed_output':False,
        'attempt_id':attempt['attempt_id'],'session_id':'old-session','task_id':'old-build','run_id':'1','phase':'page-review',
        'schema_sha256':fingerprint(schema),'wire':wire,'view':view,'extra_calls_granted':0,
        'error':{'class':'ConnectionError','message':'isolated transport failure'}}
    op=root/'call-failures/unknown.json';op.parent.mkdir(exist_ok=True);op.write_bytes(_json_bytes(outcome))
    attempt['call_outcome']={'state':'completion_unknown','report_ref':'call-failures/unknown.json','report_sha256':hashlib.sha256(op.read_bytes()).hexdigest()}
    journal={'contract':'finite-semantic-output/v1','phase':'page-review','input_sha256':fingerprint(view),
        'schema_sha256':fingerprint(schema),'attempts':[attempt]};jp.write_bytes(_json_bytes(journal))
    home=vault.parent/'native-home';home.mkdir();db=sqlite3.connect(home/'state.db')
    db.executescript('CREATE TABLE sessions(id TEXT,source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
    db.execute('INSERT INTO sessions VALUES(?,?)',('old-session','kanban'));db.execute('INSERT INTO messages VALUES(?,?,?,?)',('old-session','user',wire['user'],None));db.commit();db.close()
    monkeypatch.setattr(Path,'home',classmethod(lambda _:home.parent))
    # Native helpers use ~/.hermes; keep this isolated fixture there.
    home.rename(home.parent/'.hermes')
    ref=a.execution_failure(binding,'NATIVE_COMPLETION_UNKNOWN','isolated connection failure')
    w=a.workflow.status(w['workflow_id']);w.update(state='cancelled',cancel_requested=True);a.workflow._write(w)
    batch=a.workflow.knowledge._batch(w['batch_id']);batch.update(state='cancelled',cancel_requested=True)
    a.workflow.knowledge._batch_path(w['batch_id']).write_bytes(_json_bytes(batch))
    request=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        recovery_id='build-recovery',recovery_scope=recovery.SCOPE,target_contract_sha256=recovery.contract_hash(),reason='Explicit stopped Build recovery',
        entries=[{'node':'build-finalize','task_id':'old-build','failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest(),
            'snapshot_ref':sp.relative_to(vault).as_posix(),'snapshot_sha256':hashlib.sha256(sp.read_bytes()).hexdigest(),'retry_unknowns':[]}])
    return a,w,binding,request,jp,journal,view

def grant(request,jp,journal,vault):
    request=copy.deepcopy(request)
    request['entries'][0]['retry_unknowns']=[{'attempt_ref':jp.relative_to(vault).as_posix(),
        'attempt_sha256':fingerprint(journal['attempts'][0]),'reason':'Explicitly authorize one additional native attempt; preserve unknown'}]
    return workflow_request(**{k:v for k,v in request.items() if k!='input_digest'})

def test_default_admission_preserves_unknown_and_does_not_resume(stopped,vault):
    a,w,b,req,jp,journal,view=stopped;before=files(vault)
    preview=a.recover_build_execution(req,dry_run=True)
    assert not preview['applied'] and files(vault)==before
    result=a.recover_build_execution(req);current=a.workflow.status(w['workflow_id'])
    assert not result['resumed'] and current['state']=='cancelled'
    assert json.loads(jp.read_text())==journal
    assert a.recover_build_execution(req)['replayed']
    a.workflow.assert_execution_ready(current)
    permit=recovery.review_permit(a,b,jp,journal)
    assert permit['additional_calls']==0
    with pytest.raises(ContractError,match='no new call authorized'):
        recovery.execute_review(a,b,view,None,None,None,jp,journal,permit)
    assert json.loads(jp.read_text())==journal

@pytest.mark.parametrize('change',['actor','live','stage','snapshot','draft','failure','native-run','journal-wire','call-evidence','grant-not-unknown','grant-hash','worker'])
def test_unsafe_build_recovery_refuses_without_writes(stopped,vault,monkeypatch,change):
    a,w,b,req,jp,journal,view=stopped
    if change=='actor':req['actor']='different'
    if change=='live':monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[{'pid':1}])
    if change=='stage':w['current_stage']='reducing';a.workflow._write(w)
    if change=='snapshot':req['entries'][0]['snapshot_sha256']='0'*64
    if change=='failure':req['entries'][0]['failure_sha256']='0'*64
    if change=='native-run':a.kanban.runs['old-build'][0]['ended_at']=None
    if change=='draft':next((vault/'_system/knowledge-builds/policy-run/pages').glob('*.md')).write_text('Changed')
    if change=='journal-wire':journal['attempts'][0]['wire_sha256']='wrong';jp.write_bytes(_json_bytes(journal))
    if change=='call-evidence':next((jp.parent.parent/'call-failures').glob('*.json')).write_text('{}')
    if change in ('grant-not-unknown','grant-hash'):
        req=grant(req,jp,journal,vault)
        req['entries'][0]['retry_unknowns'][0]['attempt_sha256']='0'*64
    if change=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    req=workflow_request(**{k:v for k,v in req.items() if k!='input_digest'});before=files(vault)
    with pytest.raises(ContractError):a.recover_build_execution(req)
    assert files(vault)==before

def test_explicit_retry_appends_one_ordinal_and_reuses_it(stopped,vault,monkeypatch):
    import semantic_output
    a,w,b,req,jp,journal,view=stopped;req=grant(req,jp,journal,vault)
    a.recover_build_execution(req);permit=recovery.review_permit(a,b,jp,journal);before=copy.deepcopy(journal['attempts'])
    monkeypatch.setattr(a,'worker_check',lambda *_:{'ok':True})
    calls=[];answer={'decision':'approved','review_note':'Isolated review'}
    def model(*args,**kwargs):
        calls.append(kwargs);semantic_output.note_audit('isolated-audit.json');return answer,'isolated-audit.json'
    check=lambda x:x['decision']
    result=recovery.execute_review(a,b,view,None,model,check,jp,journal,permit)
    assert result[0]==answer and len(calls)==1 and calls[0]['attempt_limit']==1
    saved=json.loads(jp.read_text());assert saved['attempts'][:1]==before and len(saved['attempts'])==2
    monkeypatch.setattr('fixed_semantic_recovery.native_reply',lambda *_:(json.dumps(answer),{}))
    assert recovery.execute_review(a,b,view,None,model,check,jp,saved,permit)[0]==answer
    assert len(calls)==1

def test_changed_original_unknown_or_consumed_retry_cannot_mint_more_calls(stopped,vault):
    a,w,b,req,jp,journal,view=stopped;req=grant(req,jp,journal,vault);a.recover_build_execution(req)
    journal['attempts'][0]['session_id']='changed'
    with pytest.raises(ContractError):recovery.review_permit(a,b,jp,journal)

def test_recovery_report_tamper_blocks_readiness(stopped,vault):
    a,w,b,req,*_=stopped;result=a.recover_build_execution(req)
    (vault/result['report_ref']).write_text('{}')
    with pytest.raises(ContractError):a.workflow.assert_execution_ready(a.workflow.status(w['workflow_id']))
