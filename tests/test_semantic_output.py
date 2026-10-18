"""Persistent bounded correction contracts; isolated fixtures, no native acceptance."""
import copy,json
from types import SimpleNamespace
import pytest
import semantic_output as output
import fixed_semantic as fixed
import fixed_semantic_recovery as recovery
import fixed_global_packets as g
from hermes_source_units import ContractError
from test_global_plan_inputs import fixture_value


def fixture(tmp_path,monkeypatch,answers):
    value=fixture_value();state=g.initial();packet=g.next_packet(value,g.units(value),state,fixed.prepare_request_projection)
    binding={'workflow_id':'fixture','node':'global-reduce','task_id':'native-one','template_hash':'fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'execution_recovery_history':[]}),worker_check=lambda _:None)
    calls=[];saved={}
    def call(a,b,p,v,c,limit,correction=None,**kwargs):
        calls.append(copy.deepcopy(correction));answer=answers[len(calls)-1]
        if isinstance(answer,Exception):output.note_session('uncertain-native');raise answer
        ref='audit-'+str(len(calls));saved[ref]=copy.deepcopy(answer);output.note_audit(ref)
        return copy.deepcopy(answer),ref
    monkeypatch.setattr(fixed,'attach_receipt',lambda *args:None)
    monkeypatch.setattr(recovery,'native_reply',lambda a,b,path,*args: (json.dumps(saved[path.name]),{}) if path.name in saved else None)
    def run():return output.execute(adapter,binding,'global-plan',packet['view'],object(),call,
        lambda d:output.check('global-plan',value,packet,state,d),packet=packet)
    return SimpleNamespace(run=run,calls=calls,saved=saved,binding=binding,packet=packet,adapter=adapter)


def answer(bad=False):
    return {'pages':[{'identity':{'kind':'requirement','identity_key':'v'+str(n),'canonical_name':'V'+str(n),'aliases':[]},
        'path':'30_Cards/v'+str(n)+'.md','proposal_refs':['p'+str(n)]} for n in (1,2)],
        'detail_unit_refs':['u1'] if bad else [],'reason':'Isolated routing fixture'}


def test_valid_output_adds_no_call_and_is_reused(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer()]);assert c.run()[0]==answer();assert c.run()[0]==answer();assert c.calls==[None]


def test_one_precise_correction_then_restart_reuses_success(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);c.run()
    assert len(c.calls)==2 and c.calls[1]['assigned_and_requested']==['p1']
    c.binding['task_id']='fresh-after-cancel';assert c.run()[0]==answer();assert len(c.calls)==2


def test_two_failures_and_prompt_change_do_not_reset_allowance(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    for _ in range(2):
        with pytest.raises(ContractError,match='DUPLICATE'):c.run()
    assert len(c.calls)==2
    monkeypatch.setitem(fixed.RULES,'global-plan',fixed.RULES['global-plan']+' Changed prompt.')
    with pytest.raises(ContractError,match='allowance is not reset'):c.run()
    assert len(c.calls)==2


def test_correction_interrupted_after_audit_is_revalidated_without_resending(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);normal=output.write;interrupted=False
    def write(path,record):
        nonlocal interrupted
        normal(path,record)
        if len(record['attempts'])==2 and record['attempts'][1].get('audit_ref') and not interrupted:
            interrupted=True;raise RuntimeError('isolated crash after audit link')
    monkeypatch.setattr(output,'write',write)
    with pytest.raises(RuntimeError):c.run()
    assert c.run()[0]==answer() and len(c.calls)==2


def test_uncertain_transport_is_not_an_output_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[RuntimeError('connection failure')])
    with pytest.raises(RuntimeError):c.run()
    with pytest.raises(ContractError,match='completion is uncertain'):c.run()
    assert len(c.calls)==1


def test_material_refusal_and_invalid_identity_never_get_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[{'blocked_reason':'Required evidence is insufficient'}])
    with pytest.raises(ContractError,match='SEMANTIC_REVIEW_REQUIRED'):c.run()
    assert c.calls==[None]
    c.adapter.worker_check=lambda _:(_ for _ in ()).throw(ContractError('STALE_INPUT','$','changed hash'))
    with pytest.raises(ContractError,match='STALE_INPUT'):c.run()
    assert c.calls==[None]


def test_raw_native_verification_does_not_accept_changed_authority(tmp_path):
    from test_reduce_semantic_recovery import call_fixture
    c=call_fixture(tmp_path,'global-plan')
    assert recovery.native_reply(c.adapter,c.binding,c.audit_path,c.wire,c.output_schema,[],home=c.home)[0]
    c.audit['native_task_id']='unrelated';c.audit_path.write_text(json.dumps(c.audit))
    assert recovery.native_reply(c.adapter,c.binding,c.audit_path,c.wire,c.output_schema,[],home=c.home) is None


def test_real_model_call_admits_only_the_reserved_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[]);calls=[];value=fixture_value()
    def caller(phase,view,output_schema,correction=None):
        calls.append(correction)
        return json.dumps(answer(not bool(correction))),{'origin':'isolated_fixture'}
    draft,_,_=output.execute(c.adapter,c.binding,'global-plan',c.packet['view'],caller,fixed.model_call,
        lambda d:output.check('global-plan',value,c.packet,g.initial(),d),packet=c.packet)
    assert draft==answer() and len(calls)==2 and calls[1]['assigned_and_requested']==['p1']


def test_proven_uncalled_reservation_continues_same_second_ordinal(tmp_path,monkeypatch):
    import hashlib
    c=fixture(tmp_path,monkeypatch,[]);value=fixture_value();calls=[];old_guard=True
    def caller(phase,view,output_schema,correction=None):
        calls.append(correction);return json.dumps(answer(not bool(correction))),{'origin':'isolated_fixture'}
    def invoke(*args,**kwargs):
        if old_guard:kwargs['attempt_limit']=1  # reproduce the pre-SDK admission defect
        return fixed.model_call(*args,**kwargs)
    def run():return output.execute(c.adapter,c.binding,'global-plan',c.packet['view'],caller,invoke,
        lambda d:output.check('global-plan',value,c.packet,g.initial(),d),packet=c.packet)
    with pytest.raises(ContractError,match='INVALID_TRANSITION'):run()
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']);record=json.loads(path.read_text());last=record['attempts'][1]
    assert len(calls)==1 and not last.get('audit_ref') and not last.get('session_id')
    original=record['attempts'][0]['audit_ref']
    monkeypatch.setattr(recovery,'native_reply',lambda *args,**kwargs:(json.dumps(answer(True)),{}))
    report=tmp_path/'grant.json';report.write_text(json.dumps({'request':{'recovery_scope':output.SCOPE},'entries':[{'node':'global-reduce',
        'uncalled_reservation':{'journal_name':path.name,'attempt_id':last['attempt_id'],'task_id':last['task_id']}}]}))
    pointer={'report_ref':'grant.json','report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    c.adapter.workflow.status=lambda _: {'execution_recovery_history':[pointer]};c.binding['task_id']='new-after-ended-attempt';old_guard=False
    assert run()[0]==answer() and len(calls)==2
    record=json.loads(path.read_text());assert len(record['attempts'])==2 and len(record['attempts'][1]['not_called_history'])==1
