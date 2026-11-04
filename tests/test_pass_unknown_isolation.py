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
from hermes_source_units.recovery_policy import DEFAULT
from copy import deepcopy


def fixture(tmp_path, monkeypatch):
    a,b,_,_=setup(tmp_path,monkeypatch)
    a.workflow._task_passes_complete=lambda _:False
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


def test_automatic_unknown_is_immediate_and_preserves_completed_tasks(tmp_path,monkeypatch):
    from pass_unknown_isolation import automatic_entry
    a,w,i,f,e,s,p,b,d=fixture(tmp_path,monkeypatch)
    w.update(scope={'execution_mode':'auto_full','recovery_policy':deepcopy(DEFAULT)},dispatch_policy={'mode':'full'})
    i['task_ids'].append('already-complete')
    a.workflow._task_passes_complete=lambda t:t=='already-complete'
    before=(p.read_bytes(),b.read_bytes())
    entry,proof=automatic_entry(a,w,i,f)
    assert entry['task_ids']==['work-1','work-2'] and proof['new_calls_granted']==0
    assert entry['deferral']=='automatic_unknown_quarantine'
    assert before==(p.read_bytes(),b.read_bytes())
    # First attempts are sufficient; uncertainty must not consume a correction.
    assert all(r['attempts'][0]['ordinal']==1 for r in json.loads(b.read_text())['tasks'].values())


@pytest.mark.parametrize('fault',['hold','staged','unselected-canary','unaccounted-task'])
def test_automatic_policy_cannot_expand_scope(tmp_path,monkeypatch,fault):
    from pass_unknown_isolation import automatic_entry
    a,w,i,f,e,s,p,b,d=fixture(tmp_path,monkeypatch)
    w.update(scope={'execution_mode':'auto_full','recovery_policy':deepcopy(DEFAULT)},dispatch_policy={'mode':'full'})
    if fault=='hold':w['scope']['recovery_policy']['unknown_completion']='hold'
    if fault=='staged':w['scope']['execution_mode']='canary_only'
    if fault=='unselected-canary':w['dispatch_policy']={'mode':'canary','slice_ids':[]}
    if fault=='unaccounted-task':i['task_ids'].append('not-attempted')
    with pytest.raises(ContractError):automatic_entry(a,w,i,f)


def test_controller_selects_unknown_and_restart_replays_same_operation(tmp_path,monkeypatch):
    import automatic_pass_recovery as recovery
    a,w,i,f,e,s,p,b,d=fixture(tmp_path,monkeypatch)
    w.update(scope={'execution_mode':'auto_full','recovery_policy':deepcopy(DEFAULT)},dispatch_policy={'mode':'full'},
             current_stage='analyzing',batch_id='batch',actor='operator',revision=1,cancel_requested=False)
    root=tmp_path/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    card=w['kanban']['task_map'][0];card['idempotency_key']='key'
    f['input_fingerprint']='fp'
    report=root/f'reports/failed-{fingerprint(card["node"])[:16]}.json';report.parent.mkdir();report.write_text(json.dumps(f))
    binding=root/f'bindings/{fingerprint("key")}.json';binding.parent.mkdir();binding.write_text('{}')
    a.workflow.knowledge=SimpleNamespace(_slices=lambda _: [{**i,'state':'failed'}])
    a.workflow.skipped_pass_slices=lambda _:set()
    a.workflow.pending_pause=lambda _:False
    a.workflow.status=lambda _:deepcopy(w)
    monkeypatch.setattr('ingest_kanban.pass_node',lambda *_:SimpleNamespace(input_fingerprint='fp'))
    selected=recovery.actions(a,w)
    assert len(selected)==1 and selected[0]['action']=='quarantine_unknown'
    # Exercise durable intent across a crash after exclusion, before resume.
    applied={};calls=[];crash=[True]
    def skip(q):
        previous=applied.setdefault(q['operation_id'],deepcopy(q))
        assert previous==q
        calls.append(q)
    a.skip_pass_slices=skip
    def control(v,wid,command,operation):
        if command=='cancel':w['cancel_requested']=True
        elif crash.pop() if crash else False:raise OSError('interrupted before resume')
        else:w['cancel_requested']=False
    monkeypatch.setattr('native_ingest.control_workflow',control)
    monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',lambda *a,**kw:None)
    before=(p.read_bytes(),b.read_bytes())
    with pytest.raises(OSError):recovery.recover(a,w['workflow_id'])
    assert (root/'automatic-recovery/pending.json').exists()
    assert recovery.recover(a,w['workflow_id'])
    assert len(applied)==1 and len(calls)==2 and not w['cancel_requested']
    assert not (root/'automatic-recovery/pending.json').exists()
    assert before==(p.read_bytes(),b.read_bytes())
