"""Native recurrence and open worker checks stay outside the real Vault."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from ingest_kanban import KanbanCLI
from hermes_source_units import ContractError
from native_stop_inventory import workflow_processes, verify_cancel_drain


def test_program_hold_uses_schedule_and_keeps_native_identity():
    row={'id':'same-card','assignee':'ingest-program','status':'ready','block_recurrences':1}
    verbs=[]
    def invoke(args):
        verb=args[4];verbs.append(verb)
        if verb=='list':result=[dict(row)]
        elif verb=='show':result={'task':dict(row),'runs':[]}
        else:
            assert verb in ('schedule','unblock')
            row['status']='scheduled' if verb=='schedule' else 'ready'
            result={}
        return subprocess.CompletedProcess(args,0,json.dumps(result),'')
    cli=KanbanCLI(invoke)
    with cli.projection('board'):
        for _ in range(3):
            cli.wait('board','same-card','blocked','capacity or durable pause')
            assert row['status']=='scheduled' and row['block_recurrences']==1
            cli.unblock('board','same-card')
    assert 'block' not in verbs and row['id']=='same-card'


def test_unexpected_triage_is_not_treated_as_a_valid_hold():
    cli=KanbanCLI(lambda args:subprocess.CompletedProcess(args,0,
        json.dumps({'task':{'id':'t','status':'triage','assignee':'ingest-program'}}),''))
    with pytest.raises(ContractError,match='NATIVE_ROUTING_CONFLICT'):
        cli.wait('board','t','blocked','cancel')


def test_inventory_includes_open_workers_and_excludes_other_workflows(tmp_path):
    def proc(pid,args,env):
        p=tmp_path/str(pid);p.mkdir()
        (p/'cmdline').write_bytes(b'\0'.join(s.encode() for s in args)+b'\0')
        (p/'environ').write_bytes(b'\0'.join(f'{k}={v}'.encode() for k,v in env.items())+b'\0')
    proc(1,['hermes','chat'],{'HERMES_KANBAN_TASK':'unmapped','HERMES_KANBAN_BOARD':'board'})
    proc(2,['hermes','chat'],{'HERMES_KANBAN_TASK':'foreign','HERMES_KANBAN_BOARD':'other'})
    proc(3,['python','watch_ingest_workflow.py','--workflow-id','wf'],{})
    proc(4,['python','watch_ingest_workflow.py','--workflow-id','other'],{})
    assert sorted(workflow_processes('wf','board',proc_root=tmp_path),key=lambda r:r['pid'])==[
        {'pid':1,'task_id':'unmapped','kind':'native_worker'},
        {'pid':3,'task_id':None,'kind':'reconciler'}]


def test_cancel_drain_rejects_unmapped_triage_and_live_open_worker(monkeypatch):
    import native_stop_inventory as stop
    adapter=SimpleNamespace(workflow=SimpleNamespace(status=lambda _:{
        'state':'cancelled','cancel_requested':True,'kanban':{'board_id':'board'}}),
        kanban=SimpleNamespace(task_states=lambda _:{'unmapped':'triage'}))
    monkeypatch.setattr(stop,'workflow_processes',lambda *_:[])
    with pytest.raises(ContractError,match='NATIVE_ROUTING_CONFLICT'):
        verify_cancel_drain(adapter,'wf',timeout=0)
    adapter.kanban.task_states=lambda _:{'held':'scheduled'}
    monkeypatch.setattr(stop,'workflow_processes',lambda *_:[{'pid':1,'kind':'native_worker'}])
    with pytest.raises(ContractError,match='WORKER_ACTIVE'):
        verify_cancel_drain(adapter,'wf',timeout=0)
    monkeypatch.setattr(stop,'workflow_processes',lambda *_:[])
    assert verify_cancel_drain(adapter,'wf',timeout=0)['actual_consumers']==0
