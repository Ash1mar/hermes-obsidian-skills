"""Cancel a long locked native transaction without weakening request identity."""
from types import SimpleNamespace as NS
import pytest
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units import ContractError

def fixture():
    request={'workflow_id':'fixture','actor':'agent','expected_revision':7}
    current={'workflow_id':'fixture','actor':'agent','revision':7,'batch_id':None,
        'kanban':{'board_id':'board','task_map':[{'task_id':'native'}]}}
    calls=[];scheduled=[]
    def cancel(req):
        calls.append(req)
        if len(calls)==1:raise ContractError('LOCK_TIMEOUT','$','held by long native transaction')
        return {**current,'revision':8,'state':'cancelled'}
    workflow=NS(cancel=cancel,_check_request=lambda _:None,status=lambda _:current)
    adapter=NS(workflow=workflow,kanban=NS(wait=lambda *args:scheduled.append(args)))
    return adapter,request,current,calls,scheduled

def test_cancel_stops_native_holder_and_finishes_the_identical_request_once():
    a,r,w,calls,scheduled=fixture()
    result=IngestKanbanAdapter.cancel(a,r)
    assert result['state']=='cancelled' and len(calls)==2 and calls[0] is calls[1] is r
    assert scheduled[0][:3]==('board','native','scheduled')

@pytest.mark.parametrize('authorized',[True,False])
def test_cancel_yields_exact_authorized_watcher_before_lock_wait(tmp_path,monkeypatch,authorized):
    from hermes_source_units import runtime_telemetry
    a,r,w,calls,scheduled=fixture();a.workflow.vault=tmp_path;wakes=[]
    monkeypatch.setattr(runtime_telemetry,'notify_reconciler',lambda *args:wakes.append(args))
    if not authorized:r['actor']='other'
    if authorized:
        IngestKanbanAdapter.cancel(a,r)
        assert wakes==[(tmp_path,'fixture'),(tmp_path,'fixture')]
        assert calls[0] is calls[1] is r
    else:
        with pytest.raises(ContractError):IngestKanbanAdapter.cancel(a,r)
        assert not wakes and not scheduled

@pytest.mark.parametrize('change',['actor','revision','invalid-digest','foreign-error'])
def test_cancel_timeout_never_stops_workers_for_unbound_requests(change):
    a,r,w,calls,scheduled=fixture()
    if change=='actor':r['actor']='other'
    elif change=='revision':r['expected_revision']=6
    elif change=='invalid-digest':
        def invalid(_):raise ContractError('INPUT_DIGEST_MISMATCH','$','changed')
        a.workflow._check_request=invalid
    else:
        def invalid(_):raise ContractError('ACCESS_DENIED','$','not a lock timeout')
        a.workflow.cancel=invalid
    with pytest.raises(ContractError):IngestKanbanAdapter.cancel(a,r)
    assert not scheduled
