"""Adjacent Build checks retain live stop, byte and finite-output boundaries."""
import copy,json,os
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from ingest_graph_reuse import graph_scope,derive,checked_dependencies,recheck_dependencies
from test_graph_and_proof_reuse import graph_fixture
import ingest_kanban as kanban
import semantic_output as output
import fixed_semantic as fixed


@pytest.mark.parametrize('change',['bytes','add','remove','schema'])
def test_adjacent_proof_rejects_drift_without_metadata_shortcuts(tmp_path,change):
    service,w,task,slices=graph_fixture(tmp_path)
    with graph_scope():
        derive(service,w,lambda:[1]);proof=checked_dependencies(service,w)
        recheck_dependencies(service,proof)
        if change=='bytes':
            stamp=task.stat().st_mtime_ns
            task.write_text('{"value":2}');os.utime(task,ns=(stamp,stamp))
        elif change=='add':(slices/'added.json').write_text('{}')
        elif change=='remove':(slices/'s.json').unlink()
        else:proof['schema_sha256']='0'*64
        with pytest.raises(ContractError):recheck_dependencies(service,proof)
    assert checked_dependencies(service,w) is None


@pytest.mark.parametrize('change',['none','cancel','pause','binding','template','failure','same_revision_control','missing_proof'])
@pytest.mark.parametrize('read_kind',['mutable','snapshot'])
def test_build_send_control_rechecks_current_identity_and_proof(tmp_path,monkeypatch,change,read_kind):
    service,w,_,_=graph_fixture(tmp_path)
    with graph_scope():
        derive(service,w,lambda:[1]);proof=checked_dependencies(service,w)
    request={'workflow_id':'fixture','node':'build-finalize','task_id':'owner','template_hash':'pin'}
    key='ingest:fixture:build-finalize:sha256:input'
    state={'workflow_id':'fixture','batch_id':'batch','current_stage':'build_finalizing',
        'revision':1,'cancel_requested':False,'scope':{'execution_mode':'auto_full'},
        'dispatch_policy':{'mode':'full'},'kanban':{'task_map':[
            {'node':'build-finalize','task_id':'owner','idempotency_key':key}]},
        'template_pins':[{'kind':'build-finalize','template_hash':'pin'}]}
    checked={'node':'build-finalize','batch_id':'batch','input_fingerprint':'sha256:input',
        'control_fingerprint':fingerprint(state),'dependency_proof':proof}
    def pause(_):
        if change=='pause':raise ContractError('WORKFLOW_PAUSED','$','isolated pause')
    adapter=NS(enable_workers=True,validate_worker_request=lambda _:None,
        workflow=NS(vault=tmp_path,status=lambda _:copy.deepcopy(state),assert_not_paused=pause,pinned_templates=lambda _:None))
    if read_kind=='snapshot':
        from hermes_source_units.verified_json import VerifiedJsonReads
        cache=VerifiedJsonReads();state_path=tmp_path/'live-state.json';state_path.write_text(json.dumps(state))
        cache.read(state_path,readonly=True)  # Old same-revision value is warm.
        stamp=state_path.stat().st_mtime_ns
        adapter.workflow.status_snapshot=lambda _wid,origin:cache.read(state_path,readonly=True,origin=origin)
    monkeypatch.setattr(kanban,'desired_graph',lambda *_:pytest.fail('duplicate full graph'))
    if change=='cancel':state['cancel_requested']=True
    elif change=='binding':state['kanban']['task_map'][0]['task_id']='other'
    elif change=='template':state['template_pins'][0]['template_hash']='other'
    elif change=='same_revision_control':state['scope']['extra']='changed without revision'
    elif change=='missing_proof':checked.pop('dependency_proof')
    elif change=='failure':
        p=tmp_path/f'_system/ledgers/ingest-workflows/fixture/reports/failed-{fingerprint("build-finalize")[:16]}.json'
        p.parent.mkdir(parents=True);p.write_text(json.dumps({'task_id':'owner','idempotency_key':key,'error_code':'SOURCE_CHANGED'}))
    if read_kind=='snapshot':
        state_path.write_text(json.dumps(state));os.utime(state_path,ns=(stamp,stamp))
    if change=='none':kanban.IngestKanbanAdapter.worker_control_check(adapter,request,checked=checked)
    else:
        with pytest.raises(ContractError):kanban.IngestKanbanAdapter.worker_control_check(adapter,request,checked=checked)
    if read_kind=='snapshot' and change in ('cancel','pause'):
        assert cache.stats['canonical_builds']==0  # earlier stop/pause still wins


def test_fresh_build_preserves_exact_request_and_replays_without_more_calls(tmp_path,monkeypatch):
    checks=[];controls=[];calls=[]
    checked={'dependency_proof':{'fixture':True},'control_fingerprint':'fixture'}
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]}),
        worker_check=lambda _:checks.append(1) or checked,
        worker_control_check=lambda _,**kw:controls.append(kw['checked']))
    binding={'workflow_id':'fixture','node':'build-finalize','task_id':'owner'}
    view={'page':'e1','content':'unchanged original'}
    caller=object.__new__(fixed.HermesCaller)
    answer={'decision':'approved','review_note':'Isolated native fixture'}
    def call(a,b,p,v,c,limit,correction=None,**kw):
        assert kw['_checked'] is checked and v==view and correction is None
        calls.append(1);output.note_audit('audit');return answer,'audit'
    monkeypatch.setattr('fixed_semantic_recovery.native_reply',lambda *_a,**_k:(json.dumps(answer),{}))
    run=lambda:output.execute(adapter,binding,'page-review',view,caller,call,lambda d:output.check('page-review',None,None,None,d))
    run();path=output.path_for(adapter,binding,'page-review',view);before=path.read_bytes()
    journal=json.loads(before);wire=fixed.prepare_request_projection('page-review',view,output.schema_for('page-review',view),[])['wire']
    assert len(checks)==1 and len(controls)==1 and len(calls)==1
    assert journal['input_sha256']==fingerprint(view) and journal['attempts'][0]['wire_sha256']==fingerprint(wire)
    run()
    assert len(checks)==2 and len(calls)==1 and path.read_bytes()==before


def test_cancel_before_reservation_creates_no_journal_or_call(tmp_path):
    def cancel(*_a,**_k):raise ContractError('WORKFLOW_STOPPED','$','isolated cancellation')
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]}),
        worker_check=lambda _:{'dependency_proof':{}},worker_control_check=cancel)
    b={'workflow_id':'fixture','node':'build-finalize','task_id':'owner'};v={'page':'e1'}
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):
        output.execute(adapter,b,'page-review',v,object.__new__(fixed.HermesCaller),
            lambda *_a,**_k:pytest.fail('cancelled call'),lambda x:x)
    assert not output.path_for(adapter,b,'page-review',v).exists()


def test_build_model_entry_keeps_guard_until_actual_send(tmp_path,monkeypatch):
    checks=[];cancelled=False
    def control(*_a,**_k):
        checks.append(1)
        if cancelled:raise ContractError('WORKFLOW_STOPPED','$','stop after construction')
    adapter=NS(workflow=NS(vault=tmp_path),worker_control_check=control,
        worker_check=lambda _:pytest.fail('duplicate full authority check'))
    binding={'workflow_id':'fixture','node':'build-finalize','task_id':'owner'}
    caller=object.__new__(fixed.HermesCaller)
    def dispatch(*_a,prepared,**_k):
        nonlocal cancelled
        cancelled=True
        prepared['live_control_check']()
        pytest.fail('cancelled SDK dispatch')
    monkeypatch.setattr(fixed,'_dispatch_model_call',dispatch)
    with pytest.raises(ContractError,match='stop after construction'):
        fixed.model_call(adapter,binding,'page-review',{'page':'e1'},caller,30000,
            _checked={'fixture':'adjacent proof'})
    assert len(checks)==2
