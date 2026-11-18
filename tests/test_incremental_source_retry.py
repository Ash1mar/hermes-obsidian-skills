"""Source retry reuses only exact native zero-consumption jobs."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'),str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts'),str(ROOT/'hermes-source-units/src')]
import incremental_source_retry as module
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic


def fixture(tmp_path,monkeypatch):
    import run_short_evidence_test as native
    import native_stop_inventory
    import ingest_kanban
    run_id='retry-source-fixture'
    path='10_Raw/a.pdf'
    raw_hash='a'*64
    snapshot_id='snapshot-a'
    root=tmp_path/'_system/ledgers/incremental-ingest'/run_id
    workspace=root/'native/source'/snapshot_id/'mapping'
    operation={'contract':native.CONTRACT,'workflow_mode':'incremental_v1','vault':str(tmp_path),'actor':'operator','board':'retry-board','incremental':{'run_id':run_id,'scope_kind':'source','scope_id':snapshot_id,'stage':'mapping','snapshots':[snapshot_id]}}
    _write_atomic(workspace/'operation.json',_json_bytes(operation))
    row={'id':'request-a','phase':'short-topics'}
    job=workspace/'jobs/request-a.json'
    _write_atomic(job,_json_bytes({'request':row}))
    task=SimpleNamespace(id='native-a',body=native.task_body(workspace,job),status='blocked',claim_lock=None,assignee=native.ASSIGNEE,created_by=native.CONTRACT,workspace_kind='dir',workspace_path=str(workspace))
    ended=SimpleNamespace(id=3,status='gave_up',ended_at=1,outcome='gave_up')
    run={'actor':'operator','revision':7,'state':'active','sources':{path:{'state':'prepared','snapshot_id':snapshot_id}},'source_manifest':[{'path':path,'sha256':raw_hash}],'reservations':{}}
    control={'actor':'operator','revision':5,'state':'paused','active_scopes':{},'boundary':{'stage':'mapping'}}
    domain=SimpleNamespace(vault=tmp_path,root=lambda _:root,status=lambda _:run,validate_source_snapshot=lambda *_:{'original':{'ref':path,'sha256':raw_hash}})
    monkeypatch.setattr(module,'service',lambda *_:domain)
    monkeypatch.setattr(module,'IncrementalControl',lambda *_:SimpleNamespace(status=lambda:control))
    monkeypatch.setattr(module,'profile_ready',lambda:True)
    monkeypatch.setattr(module,'controller_alive',lambda _:False)
    monkeypatch.setattr(module,'completion_connection',lambda _:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(native_stop_inventory,'workflow_processes',lambda *_:[])
    kb=SimpleNamespace(list_tasks=lambda _: [task],list_runs=lambda *_a,**_k:[ended])
    monkeypatch.setattr(native,'runtime',lambda:(kb,None,None))
    unblocked=[]
    def unblock(board,tid):
        assert (board,tid)==('retry-board','native-a')
        unblocked.append(tid)
        task.status='ready'
    monkeypatch.setattr(ingest_kanban,'KanbanCLI',lambda:SimpleNamespace(unblock=unblock))
    request={'run_id':run_id,'actor':'operator','operation_id':'retry-a','control_revision':5,'run_revision':7,'source_path':path,'source_sha256':raw_hash,'retry_kind':module.KIND}
    return SimpleNamespace(domain=domain,run=run,control=control,request=request,root=root,workspace=workspace,job=job,row=row,task=task,ended=ended,unblocked=unblocked)


def test_paused_retry_unblocks_same_task_without_releasing_boundary_or_reserving_calls(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    original=item.job.read_bytes()
    assert module.preview(tmp_path,item.request['run_id'],item.request['source_path'])['allowed']
    receipt=module.retry(tmp_path,item.request)
    assert item.unblocked==['native-a']
    assert receipt['task_ids']==['native-a'] and receipt['pause_released'] is False
    assert receipt['controller_requested'] is False and item.control['state']=='paused'
    assert item.control['boundary']=={'stage':'mapping'} and item.run['reservations']=={}
    assert item.job.read_bytes()==original
    assert module.retry(tmp_path,item.request)==receipt and item.unblocked==['native-a']


@pytest.mark.parametrize('evidence',['attempt','audit','result','reservation','claim','live-controller','changed-job','unsupported-raw','permission'])
def test_retry_refuses_consumption_live_ownership_or_failed_raw_source(tmp_path,monkeypatch,evidence):
    item=fixture(tmp_path,monkeypatch)
    if evidence in ('attempt','audit','result'):
        suffix={'attempt':'.attempt-1.json','audit':'.audit-1.json','result':'.result.json'}[evidence]
        _write_atomic(item.job.with_name(item.job.stem+suffix),_json_bytes({}))
    elif evidence=='reservation':
        item.run['reservations']['reserved']={'request_fingerprint':module.fingerprint({'request':item.row['id'],'ordinal':1})}
    elif evidence=='claim':item.task.claim_lock='live'
    elif evidence=='live-controller':monkeypatch.setattr(module,'controller_alive',lambda _:True)
    elif evidence=='changed-job':_write_atomic(item.job,_json_bytes({'request':{**item.row,'view':{'changed':True}}}))
    elif evidence=='permission':item.ended.error='PermissionError: access denied'
    else:item.run['sources'][item.request['source_path']]={'state':'failed','outcome':{'error_code':'CONVERSION_FAILED'}}
    capability=module.preview(tmp_path,item.request['run_id'],item.request['source_path'])
    assert capability['allowed'] is False
    with pytest.raises((ContractError,KeyError,ValueError)):
        module.retry(tmp_path,item.request)
    assert item.unblocked==[]


def test_prepared_retry_journal_recovers_exact_unclaimed_ready_prefix(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    data=module.inventory(item.domain,item.request['run_id'],item.request['source_path'])
    journal=item.root/'source-retries/retry-a.json'
    _write_atomic(journal,_json_bytes({'contract':'incremental-source-retry/v1','request':item.request,'state':'prepared','evidence':data}))
    item.task.status='ready'
    item.run['revision']+=1
    assert module.retry(tmp_path,item.request)['task_ids']==['native-a']
    assert json.loads(journal.read_text())['state']=='committed'


def test_second_operation_cannot_replenish_same_native_task_retry(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    module.retry(tmp_path,item.request)
    item.task.status='blocked'
    request={**item.request,'operation_id':'retry-again'}
    with pytest.raises(ContractError,match='already reserved'):
        module.retry(tmp_path,request)
    assert item.unblocked==['native-a']
