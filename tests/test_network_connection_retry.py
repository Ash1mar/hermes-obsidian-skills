"""Isolated connection failures; no live API or real workflow outputs."""
import copy,json
from types import SimpleNamespace as NS
import httpx,pytest
from openai import APIConnectionError,APITimeoutError,APIStatusError
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from native_call_state import StopNativeCall
from test_semantic_request_construction import sdk,guarded_agent,api_request
import network_connection_retry as network

def fixture(sdk,tmp_path,monkeypatch,control=lambda:None,fresh=lambda:None):
    agent=guarded_agent(sdk);clock=[0.0]
    monkeypatch.setattr(network.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(network.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    agent.fixed_network_context={'vault':str(tmp_path),'binding':{'workflow_id':'fixture','task_id':'native','node':'global-reduce'},
        'phase':'page-write','wire_sha256':fingerprint(agent.fixed_text_request),'control_check':control,'fresh_check':fresh}
    agent.fixed_network_retry_refs=[];agent.fixed_network_wait_ms=0
    return agent

def connection():
    error = APIConnectionError(request=httpx.Request('POST', 'https://offline.invalid'))
    error.__cause__ = httpx.ConnectError('isolated pre-connect failure')
    return error

@pytest.mark.parametrize('status',[502,503,504])
def test_gateway_status_without_rejection_proof_is_held(sdk,tmp_path,monkeypatch,status):
    a=fixture(sdk,tmp_path,monkeypatch);calls=[];expected=api_request(a);a._apply_fixed_format(expected)
    def backend(payload):
        calls.append(copy.deepcopy(payload))
        raise APIStatusError('isolated gateway',response=httpx.Response(status,request=httpx.Request('POST','https://offline.invalid')),body=None)
    with pytest.raises(StopNativeCall):
        a._fixed_api(backend,expected)
    assert len(calls)==1 and not a.fixed_network_retry_refs

@pytest.mark.parametrize('status',[400,401,403,409,429,500])
def test_other_http_errors_do_not_gain_transport_retries(status):
    error=APIStatusError('isolated',response=httpx.Response(status,request=httpx.Request('POST','https://offline.invalid')),body=None)
    assert not network.connection_error(error)

def test_more_than_one_connection_retry_preserves_payload_and_single_semantic_reservation(sdk,tmp_path,monkeypatch):
    fresh=[];a=fixture(sdk,tmp_path,monkeypatch,fresh=lambda:fresh.append(1));request=api_request(a);a._apply_fixed_format(request)
    before=copy.deepcopy(request);calls=[];response=NS(choices=[NS(message=NS(content='native result'))])
    a.run_budget_seconds=600;a._run_budget_started_at=123
    def backend(payload):
        calls.append(copy.deepcopy(payload))
        if len(calls)<=8:raise connection()
        return response
    with pytest.raises(StopNativeCall):
        a._fixed_api(backend,request)
    assert len(calls)==3 and all(c==before for c in calls) and len(fresh)==2
    assert a.run_budget_seconds==600 and a._run_budget_started_at==123
    records=[json.loads((tmp_path/r['ref']).read_text()) for r in a.fixed_network_retry_refs]
    assert len(records)==2 and [r['network_failure_ordinal'] for r in records]==[1,2]
    assert all(r['state']=='completion_unknown' and r['semantic_ordinals_granted']==0 and not r['response_received'] for r in records)
    assert [r['next_backoff_seconds'] for r in records]==[5,10]
    assert all(fingerprint(r) for r in records) and a.fixed_network_wait_ms==15000

@pytest.mark.parametrize('error',[TimeoutError(),ConnectionError(),APITimeoutError(request=httpx.Request('POST','https://offline.invalid'))])
def test_other_unknowns_keep_existing_hold(sdk,tmp_path,monkeypatch,error):
    a=fixture(sdk,tmp_path,monkeypatch);count=[]
    def backend(_):count.append(1);raise error
    with pytest.raises(StopNativeCall):a._fixed_api(backend,api_request(a))
    assert len(count)==1 and not a.fixed_network_retry_refs

@pytest.mark.parametrize('where',['control','fresh'])
def test_control_or_changed_input_stops_before_another_dispatch(sdk,tmp_path,monkeypatch,where):
    def stop():raise ContractError('WORKFLOW_STOPPED' if where=='control' else 'SOURCE_CHANGED','$','isolated stop')
    a=fixture(sdk,tmp_path,monkeypatch,**{where:stop});count=[]
    def backend(_):count.append(1);raise connection()
    with pytest.raises(StopNativeCall) as exc:a._fixed_api(backend,api_request(a))
    assert len(count)==1 and len(a.fixed_network_retry_refs)==1
    assert exc.value.outcome['source']=='local_request_guard'

def test_possible_returned_output_and_invalid_frames_are_never_network_retries(sdk,tmp_path,monkeypatch):
    a=fixture(sdk,tmp_path,monkeypatch);error=connection();error.body={'output':['possible response']}
    with pytest.raises(StopNativeCall):a._fixed_api(lambda _:(_ for _ in ()).throw(error),api_request(a))
    with pytest.raises(StopNativeCall):a._fixed_api(lambda _:None,api_request(a))
    assert not a.fixed_network_retry_refs

def test_live_exact_request_guard_runs_before_resending(sdk,tmp_path,monkeypatch):
    a=fixture(sdk,tmp_path,monkeypatch);count=[]
    def backend(request):
        count.append(1);request['messages'].append({'role':'user','content':'changed'})
        raise connection()
    with pytest.raises(StopNativeCall,match='extra tools'):a._fixed_api(backend,api_request(a))
    assert len(count)==1 and len(a.fixed_network_retry_refs)==1
