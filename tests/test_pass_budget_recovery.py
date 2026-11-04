"""Mechanical lock recovery cannot turn uncertain model execution into a retry."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import pytest
from test_automatic_pass_recovery import setup
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock
from pass_local_review import reserve_budget, finish_budget
from pass_budget_recovery import verify, settle


def test_parallel_budget_completion_waits_without_losing_reservations(tmp_path, monkeypatch):
    a,b,v,ids = setup(tmp_path,monkeypatch)
    token = reserve_budget(a,b,'candidate',v,v['tasks'],ids)
    entered = threading.Event()
    def holder():
        with _exclusive_lock(token[0].with_suffix('.lock')):
            entered.set(); time.sleep(.15)
    thread = threading.Thread(target=holder); thread.start(); entered.wait()
    finish_budget(token,'original.json'); thread.join()
    assert all(r['attempts'] == [{'ordinal':1,'state':'returned','native_task_id':'native-1','audit_ref':'original.json'}]
               for r in json.loads(token[0].read_text())['tasks'].values())


def evidence(tmp_path, monkeypatch):
    a,b,_,_ = setup(tmp_path,monkeypatch); w={'workflow_id':b['workflow_id']}
    root=tmp_path/'_system/ledgers/ingest-workflows'/w['workflow_id']; root.mkdir(parents=True)
    timeline=tmp_path/'timeline'; timeline.mkdir()
    monkeypatch.setattr('hermes_source_units.runtime_telemetry.runtime_directory',lambda *_:timeline)
    home=tmp_path/'home'; (home/'.hermes').mkdir(parents=True)
    sqlite3.connect(home/'.hermes/state.db').close(); monkeypatch.setattr(Path,'home',lambda:home)
    e={'workflow_id':w['workflow_id'],'task_id':b['task_id'],'name':'worker_registered','native_run_id':7}
    (timeline/'123.jsonl').write_text(json.dumps(e)+'\n')
    failure={'error_code':'LOCK_BUSY','reason':f'LOCK_BUSY at $: Skill kernel lock is held: {root}/pass-call-budget.lock; wait limit=0s'}
    snapshot={'task':{'worker_pid':None},'runs':[{'id':7,'worker_pid':123,'ended_at':10}]}
    return a,b,w,failure,snapshot,timeline,root


def test_no_request_admission_requires_registered_ended_native_evidence(tmp_path, monkeypatch):
    a,b,w,f,s,t,r=evidence(tmp_path,monkeypatch)
    assert verify(a,w,b,f,s)['request_count']==0
    (t/'123.jsonl').unlink()
    with pytest.raises(ContractError,match='timeline'):verify(a,w,b,f,s)


def test_unknown_request_or_reservation_stays_held(tmp_path, monkeypatch):
    a,b,w,f,s,t,r=evidence(tmp_path,monkeypatch)
    with (t/'123.jsonl').open('a') as stream:
        stream.write(json.dumps({'workflow_id':w['workflow_id'],'task_id':b['task_id'],
                    'name':'sdk_request_start','request_id':'unknown'})+'\n')
    with pytest.raises(ContractError,match='unknown'):verify(a,w,b,f,s)
    lines=(t/'123.jsonl').read_text().splitlines(); (t/'123.jsonl').write_text(lines[0]+'\n')
    (r/'pass-call-budget.json').write_text(json.dumps({'tasks':{'k':{'attempts':[{'native_task_id':b['task_id'],'state':'reserved'}]}}}))
    with pytest.raises(ContractError,match='unconfirmed'):verify(a,w,b,f,s)


def test_settlement_is_idempotent_and_preserves_consumed_ordinal(tmp_path):
    a=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path)); p=tmp_path/'_system/ledgers/ingest-workflows/w/pass-call-budget.json'
    p.parent.mkdir(parents=True);p.write_text(json.dumps({'tasks':{'k':{'attempts':[{'ordinal':2,'native_task_id':'n','state':'reserved'}]}}}))
    proof={'settlements':{'k':'original.json'},'native_task_id':'n'}
    settle(a,'w',proof);before=p.read_bytes();settle(a,'w',proof)
    assert p.read_bytes()==before
    assert json.loads(before)['tasks']['k']['attempts'][0]['ordinal']==2


def test_watcher_selects_installed_venv_without_global_path_changes(tmp_path, monkeypatch):
    import watcher_runtime as runtime
    cli=tmp_path/'hermes';cli.write_text('wrapper');python=tmp_path/'python';python.write_text('runtime')
    monkeypatch.setattr(runtime.importlib.util,'find_spec',lambda _:None)
    monkeypatch.setattr(runtime.shutil,'which',lambda _:str(cli))
    calls=[];monkeypatch.setattr(runtime.subprocess,'run',lambda *a,**k:calls.append(a[0]))
    monkeypatch.setattr(runtime.os,'execv',lambda *a:calls.append(a))
    runtime.ensure_native_runtime()
    assert calls[0][0]==str(python) and calls[1][0]==str(python)
