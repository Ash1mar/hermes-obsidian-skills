"""Reuse pure work only; independent file and database mutations invalidate it."""
import json,sqlite3
from types import SimpleNamespace as NS
import pytest
from hermes_source_units.validation import fingerprint
from ingest_graph_reuse import graph_scope,derive
import fixed_semantic_recovery as recovery


def test_graph_reuse_tracks_contents_memberships_and_control_with_same_revision(tmp_path):
    task=tmp_path/'task.json';batch=tmp_path/'batch.json';slices=tmp_path/'slices';slices.mkdir()
    task.write_text('{}');batch.write_text('{}');(slices/'s.json').write_text('{}')
    k=NS(_batch=lambda _:{'task_ids':['t']},_batch_path=lambda _:batch,_slice_root=lambda _:slices,_task_path=lambda _:task)
    service=NS(vault=tmp_path,knowledge=k);w={'batch_id':'b','revision':1,'cancel_requested':False};built=[]
    def build():built.append(1);return [len(built)]
    with graph_scope():
        assert derive(service,w,build)==derive(service,w,build)==[1]
        task.write_text('{"changed":true}');assert derive(service,w,build)==[2]
        (slices/'new.json').write_text('{}');assert derive(service,w,build)==[3]
        w['cancel_requested']=True;assert derive(service,w,build)==[4]
    with graph_scope():assert derive(service,w,build)==[5]


def test_transcript_proof_reuses_connection_but_detects_database_update(tmp_path):
    home=tmp_path;db=sqlite3.connect(home/'state.db')
    db.executescript('CREATE TABLE sessions(id TEXT, source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
    db.execute('INSERT INTO sessions VALUES(?,?)',('s','kanban'))
    raw='{"content":"original"}';wire={'system':'JSON','user':'exact request'}
    db.executemany('INSERT INTO messages VALUES(?,?,?,?)',[('s','user',wire['user'],None),('s','assistant',raw,None)]);db.commit()
    audit={'user_sha256':fingerprint(wire['user']),'response_sha256':fingerprint(raw),'model':{'origin':'hermes_native_model','session_id':'s','response_sha256':fingerprint(raw)}}
    with recovery.proof_scope():
        assert recovery.transcript_reply(audit,wire,home=home)==raw
        assert recovery.transcript_reply(audit,wire,home=home)==raw
        db.execute("UPDATE messages SET content='changed' WHERE role='assistant'");db.commit()
        assert recovery.transcript_reply(audit,wire,home=home) is None
    db.close()


def test_batch_replay_checks_exit_authority_before_results_can_be_used(tmp_path,monkeypatch):
    checks=[];a=NS(worker_check=lambda _:checks.append(1))
    monkeypatch.setattr(recovery,'permitted_tasks',lambda *_:{})
    with recovery.authenticated_replay(a,{}):assert len(checks)==1
    assert len(checks)==2
    def changed(_):
        checks.append(1)
        if len(checks)==4:raise RuntimeError('cancelled')
    a.worker_check=changed
    with pytest.raises(RuntimeError,match='cancelled'):
        with recovery.authenticated_replay(a,{}):pass
