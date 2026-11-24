"""Reuse pure work only; independent file and database mutations invalidate it."""
import json,sqlite3,os,threading,time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
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
        stamp=task.stat().st_mtime_ns
        task.write_text('{"changed":true}');os.utime(task,ns=(stamp,stamp))
        assert derive(service,w,build)==[2]
        (slices/'new.json').write_text('{}');assert derive(service,w,build)==[3]
        w['cancel_requested']=True;assert derive(service,w,build)==[4]
    with graph_scope():assert derive(service,w,build)==[5]


def test_adjacent_fingerprint_reuse_keeps_canonical_identity_and_later_mutation_check(tmp_path,monkeypatch):
    import ingest_graph_reuse as reuse
    service,w,_,_=graph_fixture(tmp_path);calls=[];real=reuse.fingerprint
    def counted(value):calls.append(1);return real(value)
    monkeypatch.setattr(reuse,'fingerprint',counted)
    with graph_scope() as scope:
        assert derive(service,w,lambda:['node'])==['node']
        control=reuse.workflow_fingerprint(w)
        proof=reuse.checked_dependencies(service,w,canonical=control)
        assert proof and control==fingerprint(w) and len(calls)==2
        assert scope['fingerprint_reuses']==2
        w['cancel_requested']=True
        assert reuse.checked_dependencies(service,w) is None
        assert len(calls)==3


def graph_fixture(tmp_path):
    from hermes_source_units.source_units import _load_json
    batch=tmp_path/'batch.json';batch.write_text('{"task_ids":["t"]}')
    task=tmp_path/'task.json';task.write_text('{"value":1}')
    slices=tmp_path/'slices';slices.mkdir();(slices/'s.json').write_text('{}')
    k=NS(_batch=lambda _:_load_json(batch),_batch_path=lambda _:batch,
         _slice_root=lambda _:slices,_task_path=lambda _:task)
    return NS(vault=tmp_path,knowledge=k),{'workflow_id':'fixture','batch_id':'b','revision':1},task,slices


def wait_pending(scope,count):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        with scope['condition']:
            if sum(len(x['pending']) for x in scope['lanes'].values())==count:return
        time.sleep(.005)
    pytest.fail('cohort registration did not settle')


def test_scan_closes_before_io_and_late_arrivals_share_only_next_scan(tmp_path,monkeypatch):
    import ingest_graph_reuse as reuse
    from hermes_source_units.source_units import _load_json
    service,w,task,_=graph_fixture(tmp_path)
    real=reuse.dependencies;entered=[threading.Event(),threading.Event()]
    release=[threading.Event(),threading.Event()];lookups=[]
    def scan(*args,**kwargs):
        if kwargs['scan_role']=='lookup':
            lookups.append(kwargs['shared_callers']);n=len(lookups)-1
            if n<2:
                entered[n].set();assert release[n].wait(5)
        return real(*args,**kwargs)
    monkeypatch.setattr(reuse,'dependencies',scan)
    def build():return [_load_json(task)['value']]
    with graph_scope() as scope,ThreadPoolExecutor(max_workers=4) as pool:
        first=pool.submit(copy_context().run,derive,service,w,build)
        assert entered[0].wait(5)
        second=[pool.submit(copy_context().run,derive,service,w,build) for _ in range(2)]
        wait_pending(scope,2);release[0].set();assert first.result(timeout=5)==[1]
        assert entered[1].wait(5)
        # Same revision/size/mtime change occurs before the next cohort reads.
        stamp=task.stat().st_mtime_ns;task.write_text('{"value":2}');os.utime(task,ns=(stamp,stamp))
        late=pool.submit(copy_context().run,derive,service,w,build)
        wait_pending(scope,1);release[1].set()
        assert [f.result(timeout=5) for f in second]==[[2],[2]]
        assert late.result(timeout=5)==[2]
        assert lookups==[1,2,1] and scope['builds']==2


def test_captured_build_does_not_reread_and_mutation_never_gets_published(tmp_path,monkeypatch):
    from pathlib import Path
    from hermes_source_units.source_units import _load_json
    from hermes_source_units import ContractError
    service,w,task,slices=graph_fixture(tmp_path);reads=[];real=Path.read_bytes
    def counted(path):
        if path==task:reads.append(1)
        return real(path)
    monkeypatch.setattr(Path,'read_bytes',counted)
    def build():
        first=_load_json(task);first['value']=99
        assert _load_json(task)['value']==1
        return [1]
    with graph_scope():
        assert derive(service,w,build)==[1] and len(reads)==2
        # Every later lookup hashes actual bytes and enumerates actual members.
        assert derive(service,w,build)==[1] and len(reads)==3
        (slices/'s.json').unlink()
        assert derive(service,w,build)==[1] and len(reads)==5
    def changing():
        task.write_text('{"value":2}')
        assert _load_json(task)['value']==1
        return [1]
    with graph_scope():
        with pytest.raises(ContractError,match='SOURCE_CHANGED'):derive(service,w,changing)
        assert derive(service,w,lambda:[_load_json(task)['value']])==[2]


def test_failed_cohort_wakes_all_waiters_and_later_lookup_checks_again(tmp_path,monkeypatch):
    import ingest_graph_reuse as reuse
    from hermes_source_units import ContractError
    service,w,_,_=graph_fixture(tmp_path);real=reuse.dependencies
    entered=threading.Event();release=threading.Event();lookups=[]
    def scan(*args,**kwargs):
        if kwargs['scan_role']=='lookup':
            lookups.append(1)
            if len(lookups)==1:entered.set();assert release.wait(5)
            if len(lookups)==2:raise ContractError('SOURCE_UNAVAILABLE','$','isolated read failure')
        return real(*args,**kwargs)
    monkeypatch.setattr(reuse,'dependencies',scan)
    with graph_scope() as scope,ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(copy_context().run,derive,service,w,lambda:[1])
        assert entered.wait(5)
        rest=[pool.submit(copy_context().run,derive,service,w,lambda:[1]) for _ in range(2)]
        wait_pending(scope,2);release.set();assert first.result(timeout=5)==[1]
        for f in rest:
            with pytest.raises(ContractError,match='SOURCE_UNAVAILABLE'):f.result(timeout=5)
        assert derive(service,w,lambda:[1])==[1] and len(lookups)==3


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


def test_managed_transcript_requires_the_exact_full_rows_and_raw_tool_evidence(tmp_path, monkeypatch):
    assembly = pytest.importorskip('persistence_assembly')
    constants = pytest.importorskip('hermes_constants')
    messages = pytest.importorskip('persistence.conversation.messages')
    import session_transcript_reader as reader
    from session_usage_reader import SessionStorageUnavailable

    profile = tmp_path / 'active-profile'
    profile.mkdir()
    database = tmp_path / 'declared-sessions.sqlite'
    monkeypatch.setattr(constants, 'get_hermes_home', lambda: profile)
    monkeypatch.setattr(assembly, 'profile_scope', lambda *, home: home)
    monkeypatch.setattr(assembly, 'store_location', lambda scope: database if scope == profile else None)
    session_id = 'fixed-full-native-session-id'
    wire = {'system': 'JSON', 'user': 'exact request'}
    raw = '{"content":"original"}'
    digest = fingerprint(raw)
    audit = {'user_sha256': fingerprint(wire['user']), 'response_sha256': digest, 'model': {
        'origin': 'hermes_native_model', 'session_id': session_id, 'response_sha256': digest,
    }}
    records = [NS(id=1, session_id=session_id, role='user', content=wire['user'], tool_calls=None),
               NS(id=2, session_id=session_id, role='assistant', content=raw, tool_calls=None),
               NS(id=3, session_id=session_id, role='assistant', content='inactive', tool_calls=[])]
    opened = []
    requested = []
    visible = list(records)
    fail_read = False

    def read_transcript(identity, request):
        requested.append(identity)
        assert request.view is messages.TranscriptView.ALL and request.limit is None and not request.include_lineage
        if fail_read:
            raise SessionStorageUnavailable('cannot establish managed transcript')
        return visible

    @contextmanager
    def open_reader(home):
        opened.append(home)
        yield NS(sessions=NS(get=lambda _: NS(session_id=session_id, source='kanban')),
                 messages=NS(read_transcript=read_transcript))

    monkeypatch.setattr(reader, 'managed_session_reader', open_reader)
    with closing(sqlite3.connect(database)) as db:
        db.executescript('CREATE TABLE sessions(id TEXT, source TEXT); '
                         'CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, '
                         'content TEXT, tool_calls TEXT, active INTEGER, compacted INTEGER);')
        db.execute('INSERT INTO sessions VALUES (?, ?)', (session_id, 'kanban'))
        db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)', [
            (row.id, row.session_id, row.role, row.content, None, int(row.id < 3), int(row.id == 3))
            for row in records
        ])
        db.commit()
        with recovery.proof_scope():
            assert recovery.transcript_reply(audit, wire) == raw
            prefix = {**audit, 'model': {**audit['model'], 'session_id': session_id[:9]}}
            assert recovery.transcript_reply(prefix, wire) is None
            assert requested == [session_id]
            for stored_tool_calls in ('null', '{"broken"'):
                db.execute('UPDATE messages SET tool_calls=? WHERE id=3', (stored_tool_calls,))
                db.commit()
                assert recovery.transcript_reply(audit, wire) is None
            db.execute("UPDATE messages SET tool_calls='[]' WHERE id=3")
            db.commit()
            visible.pop()
            assert recovery.transcript_reply(audit, wire) is None
            visible.append(records[-1])
            assert recovery.transcript_reply(audit, wire) == raw
            fail_read = True
            with pytest.raises(SessionStorageUnavailable, match='cannot establish managed transcript'):
                recovery.transcript_reply(audit, wire)
    assert opened == [profile]


def test_batch_replay_checks_exit_authority_before_results_can_be_used(tmp_path,monkeypatch):
    checks=[];a=NS(worker_check=lambda _:checks.append(1))
    monkeypatch.setattr(recovery,'permitted_tasks',lambda *_a,**_kw:{})
    with recovery.authenticated_replay(a,{}):assert len(checks)==1
    assert len(checks)==2
    def changed(_):
        checks.append(1)
        if len(checks)==4:raise RuntimeError('cancelled')
    a.worker_check=changed
    with pytest.raises(RuntimeError,match='cancelled'):
        with recovery.authenticated_replay(a,{}):pass
