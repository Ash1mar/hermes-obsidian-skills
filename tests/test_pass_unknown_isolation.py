import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from test_automatic_pass_recovery import setup
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from pass_unknown_isolation import verify


def fixture(tmp_path, monkeypatch):
    a,b,_,_=setup(tmp_path,monkeypatch)
    w={'workflow_id':b['workflow_id'],'kanban':{'board_id':'board','task_map':[{'node':b['node'],'task_id':b['task_id']}]}}
    item={'slice_id':'one','task_ids':['work-1','work-2']}
    snap={'task':{},'runs':[{'id':7,'worker_pid':123,'ended_at':10}]}
    a.kanban=SimpleNamespace(task_snapshot=lambda *_:snap)
    root=tmp_path/'_system/ledgers/ingest-workflows'/w['workflow_id'];path=root/'call-failures/unknown.json';path.parent.mkdir(parents=True)
    wire={'system':'rules','user':'request'}
    call={'contract':'native-call-outcome/v1','state':'completion_unknown','session_id':'native-session',
          'task_id':b['task_id'],'run_id':'7','phase':'citation','completed_output':False,'extra_calls_granted':0,
          'wire':wire,'wire_sha256':fingerprint(wire)}
    path.write_text(json.dumps(call))
    failure={'error_code':'NATIVE_COMPLETION_UNKNOWN','task_id':b['task_id'],'node':b['node'],
             'reason':'NATIVE_COMPLETION_UNKNOWN at $: '+json.dumps({'report':str(path),'session_id':'native-session'})}
    entry={'acknowledge_unverified_coverage':True,'unknown_call':{'ref':path.relative_to(tmp_path).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
    budget=root/'pass-call-budget.json';budget.write_text(json.dumps({'tasks':{t:{'task_id':t,'phase':'citation','attempts':[{'native_task_id':b['task_id'],'state':'reserved','ordinal':1}]} for t in item['task_ids']}}))
    home=tmp_path/'home';(home/'.hermes').mkdir(parents=True);monkeypatch.setattr(Path,'home',lambda:home)
    dbpath=home/'.hermes/state.db'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE sessions(id TEXT,source TEXT)');db.execute('INSERT INTO sessions VALUES (?,?)',('native-session','kanban'))
        db.execute('CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT)')
        db.execute('INSERT INTO messages VALUES (?,?,?,?)',('native-session','user','request','[]'))
    return a,w,item,failure,entry,snap,path,budget,dbpath


def test_explicit_quarantine_authenticates_without_changing_unknown_or_budget(tmp_path,monkeypatch):
    a,w,i,f,e,s,p,b,d=fixture(tmp_path,monkeypatch);before=(p.read_bytes(),b.read_bytes())
    proof=verify(a,w,i,f,e)
    assert proof['new_calls_granted']==0 and not proof['verification_success']
    assert proof['task_ids']==i['task_ids'] and before==(p.read_bytes(),b.read_bytes())


@pytest.mark.parametrize('fault',['authorization','live','hash','binding','returned','reservation'])
def test_quarantine_rejects_unsafe_or_ambiguous_scope(tmp_path,monkeypatch,fault):
    a,w,i,f,e,s,p,b,d=fixture(tmp_path,monkeypatch)
    if fault=='authorization':e['acknowledge_unverified_coverage']=False
    if fault=='live':s['task']['worker_pid']=123
    if fault=='hash':e['unknown_call']['sha256']='0'*64
    if fault=='binding':f['task_id']='foreign'
    if fault=='returned':
        with sqlite3.connect(d) as db:db.execute('INSERT INTO messages VALUES (?,?,?,?)',('native-session','assistant','actual response','[]'))
    if fault=='reservation':b.write_text(json.dumps({'tasks':{}}))
    with pytest.raises(ContractError):verify(a,w,i,f,e)
