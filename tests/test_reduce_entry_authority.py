"""Isolated entry/stop races, never native domain products."""
import copy,json,threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
import pytest
import fixed_semantic as fixed
import ingest_kanban as kanban
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint


@pytest.mark.parametrize('capacity',[6,10])
def test_page_entry_checks_overlap_without_shared_action_lock(tmp_path,capacity):
    barrier=threading.Barrier(capacity)
    def check(_):
        assert not fixed.ACTION_LOCK._is_owned()
        barrier.wait(timeout=5)
    adapter=NS(workflow=NS(vault=tmp_path),worker_check=check)
    binding={'workflow_id':'fixture','node':'global-reduce','task_id':'owner'}
    def call(n):
        return fixed.model_call(adapter,binding,'page-write',{'fixture':n},
            lambda *_:('{"content":"isolated fixture"}',{'origin':'isolated'}),30000)
    with ThreadPoolExecutor(max_workers=capacity) as pool:
        assert len(list(pool.map(call,range(capacity))))==capacity


def test_other_phase_entry_keeps_existing_action_lock(tmp_path):
    def check(_):assert fixed.ACTION_LOCK._is_owned()
    adapter=NS(workflow=NS(vault=tmp_path),worker_check=check)
    fixed.model_call(adapter,{'workflow_id':'fixture','node':'build-finalize','task_id':'owner'},
        'page-review',{},lambda *_:('{"decision":"approved","review_note":"fixture"}',{}),30000)


def test_full_global_check_does_not_treat_shared_graph_as_live_permission(tmp_path):
 request={'workflow_id':'fixture','node':'global-reduce'}
 def stopped(*_a,**_k):raise ContractError('WORKFLOW_STOPPED','$','cancel while waiting for shared scan')
 adapter=NS(workflow=NS(vault=tmp_path),validate_worker_request=lambda _:None,
     _batch_worker=lambda _:(dict(batch_id='b'),NS(name='global-reduce',input_fingerprint='input')),
     worker_control_check=stopped)
 with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):
  kanban.IngestKanbanAdapter.worker_check(adapter,request)


@pytest.mark.parametrize('change',['none','cancel','pause','binding','template','failure'])
def test_send_time_control_rechecks_live_state_without_dependency_graph(tmp_path,monkeypatch,change):
    request={'workflow_id':'fixture','node':'global-reduce','task_id':'owner','template_hash':'pin'}
    checked={'batch_id':'batch','input_fingerprint':'sha256:input'}
    key='ingest:fixture:global-reduce:sha256:input'
    state={'workflow_id':'fixture','batch_id':'batch','cancel_requested':False,
        'scope':{'execution_mode':'auto_full'},'dispatch_policy':{'mode':'full'},
        'kanban':{'task_map':[{'node':'global-reduce','task_id':'owner','idempotency_key':key}]},
        'template_pins':[{'kind':'global-reduce','template_hash':'pin'}]}
    def pause(_):
        if change=='pause':raise ContractError('WORKFLOW_PAUSED','$','fixture pause')
    adapter=NS(enable_workers=True,validate_worker_request=lambda _:None,
        workflow=NS(vault=tmp_path,status=lambda _:copy.deepcopy(state),assert_not_paused=pause,pinned_templates=lambda _:None))
    monkeypatch.setattr(kanban,'desired_graph',lambda *_:pytest.fail('live guard must not rebuild graph'))
    if change=='cancel':state['cancel_requested']=True
    elif change=='binding':state['kanban']['task_map'][0]['task_id']='new-owner'
    elif change=='template':state['template_pins'][0]['template_hash']='changed'
    elif change=='failure':
        path=tmp_path/f'_system/ledgers/ingest-workflows/fixture/reports/failed-{fingerprint("global-reduce")[:16]}.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'task_id':'owner','idempotency_key':key,'error_code':'SOURCE_CHANGED'}))
    if change=='none':kanban.IngestKanbanAdapter.worker_control_check(adapter,request,checked=checked)
    else:
        with pytest.raises(ContractError):kanban.IngestKanbanAdapter.worker_control_check(adapter,request,checked=checked)
