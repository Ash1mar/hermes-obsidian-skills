"""Isolated call-state and retry boundaries; no API/native semantic acceptance."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
import native_call_state as calls
from test_semantic_request_construction import sdk,guarded_agent,api_request


class Rejected(Exception):
    status_code=400
    body={'type':'invalid_request_error','message':'Invalid request parameter','param':'input'}


@pytest.mark.parametrize('error,state,retry',[(Rejected(),'request_rejected',False),(TimeoutError(),'completion_unknown',False),
    (NS(body={},status_code=429),'request_rejected',True),(NS(body={'type':'invalid_request_error','partial':True},status_code=400),'completion_unknown',False)])
def test_state_uses_structured_rejection_not_zero_usage_or_error_message(error,state,retry):
    result=calls.classify(error);assert result['state']==state and result['retryable'] is retry
    assert calls.classify(RuntimeError('request invalid; tokens=0'))['state']=='completion_unknown'


def test_deterministic_error_escapes_sdk_blanket_retry_first_time(sdk):
    agent=guarded_agent(sdk);count=0
    def rejected(*_):
        nonlocal count
        count+=1;raise Rejected()
    with pytest.raises(calls.StopNativeCall) as exc:
        for _ in range(3):
            try:agent._fixed_api(rejected,api_request(agent))
            except Exception:continue
    assert count==1 and exc.value.outcome['state']=='request_rejected'


def test_unknown_completion_also_stops_duplicate_generation(sdk):
    agent=guarded_agent(sdk)
    with pytest.raises(calls.StopNativeCall) as exc:agent._fixed_api(lambda *_:(_ for _ in ()).throw(TimeoutError()),api_request(agent))
    assert exc.value.outcome['state']=='completion_unknown'


@pytest.mark.parametrize('response',[None,NS(invalid=True,details=['empty choices'],choices=[],usage={'output_tokens':0})])
def test_returned_invalid_response_escapes_native_retry_without_new_ordinal(sdk,response):
    agent=guarded_agent(sdk);count=0
    def empty(*_):
        nonlocal count
        count+=1;return response
    with pytest.raises(calls.StopNativeCall) as exc:
        for _ in range(3):
            try:agent._fixed_api(empty,api_request(agent))
            except Exception:continue
    assert count==1
    assert exc.value.outcome['state']=='completion_unknown' and not exc.value.outcome['retryable']
    assert exc.value.outcome['source']=='native_response_validation'
    assert exc.value.outcome['response_evidence_sha256']==fingerprint(exc.value.outcome['response_evidence'])


def test_valid_native_frame_keeps_semantic_json_failure_for_existing_finite_validator(sdk):
    agent=guarded_agent(sdk);response=NS(choices=[NS(message=NS(content='not JSON'))])
    assert agent._fixed_api(lambda *_:response,api_request(agent)) is response


def test_final_codex_frame_is_rechecked_after_conversion(sdk,monkeypatch):
    agent=guarded_agent(sdk);base=type(agent).__mro__[1];seen=[]
    def native_frame(_self,request,**kw):
        seen.append(copy.deepcopy(request));return NS(output=[{'type':'message','content':'native frame'}])
    monkeypatch.setattr(base,'_run_codex_stream',native_frame,raising=False)
    request=api_request(agent,form='responses');agent._apply_fixed_format(request);agent._run_codex_stream(request)
    assert len(seen)==1 and seen[0]['text']['format']['strict'] is True
    request['input'].append({'role':'user','content':'extra'})
    with pytest.raises(calls.StopNativeCall,match='extra tools'):agent._run_codex_stream(request)
    assert len(seen)==1


def test_failure_evidence_does_not_change_ordinal_or_turn_into_output(tmp_path):
    import semantic_output as out
    root=tmp_path/'_system/ledgers/ingest-workflows/f';path=root/'output-attempts/j.json'
    old={'attempt':2,'attempt_id':'a2','task_id':'native','run_id':'ended','session_id':'s','state':'reserved'}
    journal={'phase':'page-write','schema_sha256':'schema','attempts':[{'attempt':1,'state':'rejected'},copy.deepcopy(old)]}
    token=out.ACTIVE.set((path,journal,1))
    try:
        with pytest.raises(ContractError,match='NATIVE_REQUEST_REJECTED'):
            calls.record(calls.classify(Rejected()),{'wire':{'system':'JSON','user':'native input'}},'s',request_sha256='final')
    finally:out.ACTIVE.reset(token)
    saved=json.loads(path.read_text());assert len(saved['attempts'])==2
    assert {k:v for k,v in saved['attempts'][1].items() if k!='call_outcome'}==old
    report=json.loads((root/saved['attempts'][1]['call_outcome']['report_ref']).read_text())
    assert report['completed_output'] is False and report['extra_calls_granted']==0


@pytest.mark.parametrize('change',[None,'wire','no-structured-error','actual-output','active-run'])
def test_recovery_authenticates_legacy_backend_rejection_and_preserves_reserved_attempt(tmp_path,monkeypatch,change):
    import sqlite3
    from pathlib import Path
    monkeypatch.setattr(Path,'home',classmethod(lambda _:tmp_path))
    home=tmp_path/'.hermes';(home/'sessions').mkdir(parents=True)
    db=sqlite3.connect(home/'state.db');db.executescript('CREATE TABLE sessions(id TEXT,source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
    view={'candidate':'exact'};schema={'type':'object'}
    wire={'system':'Only JSON','user':'Semantic input v1\n'+json.dumps({'phase':'page-write','input':view,'output_schema':schema})}
    db.execute('INSERT INTO sessions VALUES(?,?)',('s','kanban'))
    db.executemany('INSERT INTO messages VALUES(?,?,?,?)',[('s','user',wire['user'],None),('s','assistant','actual model output' if change=='actual-output' else 'Your request was not processed. Send it again if you still want me to carry it out.',None)]);db.commit();db.close()
    body={'instructions':wire['system'],'input':[{'role':'user','content':wire['user']}], 'text':{'format':{'type':'json_object'}}}
    if change=='wire':body['input'][0]['content']+=' different'
    dump={'session_id':'s','request':{'body':body},'error':{'type':'invalid_request_error','message':'Invalid unrelated parameter'}}
    if change=='no-structured-error':dump['error']={'message':'unknown error; tokens=0'}
    (home/'sessions/request_dump_s_1.json').write_text(json.dumps(dump))
    root=tmp_path/'_system/ledgers/ingest-workflows/f';p=root/'output-attempts/a.json';p.parent.mkdir(parents=True)
    j={'contract':'finite-semantic-output/v1','phase':'page-write','input_sha256':fingerprint(view),'schema_sha256':fingerprint(schema),
        'attempts':[{'task_id':'native','attempt_id':'a','attempt':1,'run_id':'1','session_id':'s','wire_sha256':fingerprint(wire),'state':'reserved'}]}
    p.write_text(json.dumps(j));before=p.read_bytes();a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'f','task_id':'native'}
    runs=[{'id':1,'ended_at':None if change=='active-run' else 2}]
    if change is None:
        result=calls.recovery_evidence(a,{},b,runs)
        assert result['new_calls_granted']==0 and result['reserved_attempts_preserved'] is True
        assert result['native_call_outcomes'][0]['same_attempt_retry_authorized'] is False
    else:
        with pytest.raises(ContractError):calls.recovery_evidence(a,{},b,runs)
    assert p.read_bytes()==before
