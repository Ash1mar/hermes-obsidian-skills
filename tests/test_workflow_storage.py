"""Storage representation equivalence, fresh control reads and crash recovery.

All faults and inode protection are confined to pytest's isolated directory.
"""
import copy,hashlib,json,os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units import workflow_storage as storage,evidence_snapshot as sealed
from hermes_source_units import ingest_workflow as workflow
from hermes_source_units.ingest_workflow import FileIngestWorkflowService,mutation_digest
from hermes_source_units.source_units import _load_json,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.record_snapshot import record_snapshot
from hermes_source_units.read_accounting import accounting_scope

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux immutable segment contract')


@pytest.fixture
def store(tmp_path,monkeypatch):
    def shape(kind,value):
        assert storage.MARKER not in value
        if type(value.get('cancel_requested')) is not bool:raise ContractError('INVALID_SCHEMA','$','cancel shape')
    monkeypatch.setattr(workflow,'validate_record',shape);monkeypatch.setattr(storage,'validate_record',shape)
    service=object.__new__(FileIngestWorkflowService);service.vault=tmp_path
    service.knowledge=NS(_slices=lambda _:[],_batch_path=lambda _:tmp_path/'batch.json')
    path=service._path('ingest-storage');path.parent.mkdir(parents=True)
    state={'workflow_id':path.stem,'actor':'operator','revision':7,'state':'cancelled','cancel_requested':True,
        'batch_id':'batch','current_stage':'build_finalizing','kanban':{'board_id':None,'task_map':[]},
        'scope':{},'pass_revision':{'state':'accepted','revision_id':'current','input_digest':'sha256:'+'1'*64,
            'protected_files':[{'ref':'source/a','sha256':'a'*64},{'ref':'source/a','sha256':'a'*64}]},
        'pass_revision_history':[{'state':'awaiting_review','revision_id':'old','payload':['same']*30},
            {'state':'accepted','revision_id':'older','payload':['other']*30}]}
    path.write_bytes(_json_bytes(state));(tmp_path/'batch.json').write_text('{"task_ids":[]}')
    s=NS(service=service,path=path,state=state)
    def request(mode='segments',operation='fixture'):
        w=service._load(path.stem)
        r={'workflow_id':path.stem,'actor':w['actor'],'expected_revision':w['revision'],
            'operation_id':operation,'mode':mode,'expected_root_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'logical_fingerprint':fingerprint(w)}
        r['input_digest']=mutation_digest(r);return r
    s.request=request
    yield s
    # Restore isolated inode flags before pytest removes its own temp data.
    for directory in [*tmp_path.rglob('pass-history'),*tmp_path.rglob('pass-protected')]:
        for generation in directory.iterdir():
            if generation.is_dir():
                sealed.flags(generation,sealed.flags(generation)&~sealed.IMMUTABLE)
                for file in generation.iterdir():sealed.flags(file,sealed.flags(file)&~sealed.IMMUTABLE)


def migrate(s):return s.service.migrate_storage(s.request())


def test_compact_then_segments_preserve_values_identity_order_and_review_states(store):
    s=store;old=s.path.read_bytes();h=fingerprint(s.state)
    s.service.migrate_storage(s.request('compact','compact'))
    assert len(s.path.read_bytes())<len(old) and _load_json(s.path)==s.state
    request=s.request();receipt=s.service.migrate_storage(request)
    assert receipt['logical_fingerprint']==h and receipt['revision']==7 and not receipt['resumed']
    assert _load_json(s.path)==s.state and s.service._load(s.path.stem)==s.state
    assert s.service.migrate_storage(request)['replayed']
    physical=json.loads(s.path.read_bytes());assert physical['pass_revision_history']==[]
    refs=storage.segment_paths(s.path,physical);assert len(refs)==2
    assert all(sealed.flags(p)&sealed.IMMUTABLE and sealed.flags(p.parent)&sealed.IMMUTABLE for p,_ in refs)
    assert _load_json(s.path)['pass_revision_history'][0]['state']=='awaiting_review'
    with record_snapshot({s.path:(s.path.read_bytes(),hashlib.sha256(s.path.read_bytes()).digest())}):
        assert _load_json(s.path)==s.state


def test_cached_projection_is_detached_and_cancel_without_revision_is_fresh(store):
    s=store;migrate(s)
    with s.service.read_projection():
        first=s.service.status_fields(s.path.stem,('pass_revision_history',));first['pass_revision_history'][0].clear()
        assert s.service.status_fields(s.path.stem,('pass_revision_history',))['pass_revision_history']==s.state['pass_revision_history']
        snap=s.service.status_snapshot(s.path.stem,origin='fixture')
        assert snap.canonical_fingerprint==fingerprint(s.state)
        root=json.loads(s.path.read_bytes());root['cancel_requested']=False
        stamp=s.path.stat().st_mtime_ns;s.path.write_bytes(storage.compact(root));os.utime(s.path,ns=(stamp,stamp))
        assert s.service.status_fields(s.path.stem,('cancel_requested',))['cancel_requested'] is False
        root['cancel_requested']=None;s.path.write_bytes(storage.compact(root))
        with pytest.raises(ContractError,match='INVALID_SCHEMA'):s.service._load(s.path.stem)


@pytest.mark.parametrize('fault',['replace','missing','symlink'])
def test_cached_root_cannot_hide_bad_history(store,fault):
    s=store;migrate(s);root=json.loads(s.path.read_bytes());target=storage.segment_paths(s.path,root)[0][0]
    cache=VerifiedJsonReads();assert cache.read(s.path)==s.state
    raw=target.read_bytes();stamp=target.stat().st_mtime_ns
    sealed.flags(target.parent,sealed.flags(target.parent)&~sealed.IMMUTABLE)
    sealed.flags(target,sealed.flags(target)&~sealed.IMMUTABLE)
    if fault=='replace':target.write_bytes(raw.replace(b'old',b'new'));os.utime(target,ns=(stamp,stamp))
    elif fault=='missing':target.unlink()
    else:target.unlink();target.symlink_to(s.path)
    try:
        with pytest.raises(ContractError,match='WORKFLOW_STORAGE_INVALID'):cache.read(s.path)
    finally:
        if target.is_symlink():target.unlink()
        if not target.exists():target.write_bytes(raw)


def test_logical_writer_preserves_prefix_and_allows_ordered_inline_append(store):
    s=store;migrate(s);root=json.loads(s.path.read_bytes())
    value=s.service._load(s.path.stem);value['revision']+=1;value['cancel_requested']=False
    value['pass_revision_history'].append(copy.deepcopy(value['pass_revision_history'][0]))
    s.service._write(value)
    assert _load_json(s.path)==value
    current=json.loads(s.path.read_bytes())
    assert current[storage.MARKER]==root[storage.MARKER] and len(current['pass_revision_history'])==1
    with pytest.raises(ContractError,match='logical workflow writer'):_write_atomic(s.path,_json_bytes(value))
    value['pass_revision_history'][0]['state']='accepted'
    with pytest.raises(ContractError,match='cannot modify'):s.service._write(value)


@pytest.mark.parametrize('when',['before_publication','after_publication'])
def test_crash_is_replayable_without_two_authorities(store,monkeypatch,when):
    s=store;request=s.request();old=s.path.read_bytes();atomic=storage._atomic_root
    def crash(path,data):
        if when=='after_publication':atomic(path,data)
        raise OSError('isolated crash')
    monkeypatch.setattr(storage,'_atomic_root',crash)
    with pytest.raises(OSError,match='isolated crash'):s.service.migrate_storage(request)
    if when=='before_publication':assert s.path.read_bytes()==old
    assert _load_json(s.path)==s.state
    monkeypatch.setattr(storage,'_atomic_root',atomic)
    assert s.service.migrate_storage(request)['ok'] and _load_json(s.path)==s.state


def test_wrong_raw_hash_or_logical_identity_never_publishes(store):
    s=store;old=s.path.read_bytes()
    for name in ('expected_root_sha256','logical_fingerprint'):
        r=s.request();r[name]='0'*64;r['input_digest']=mutation_digest(r)
        with pytest.raises(ContractError,match='WORKFLOW_STORAGE_INVALID'):s.service.migrate_storage(r)
        assert s.path.read_bytes()==old


def test_supported_inline_recovery_preserves_identity_before_old_code_restore(store):
    s=store;migrate(s)
    s.service.migrate_storage(s.request('inline','inline-recovery'))
    assert storage.MARKER not in json.loads(s.path.read_bytes())
    assert _load_json(s.path)==s.state and fingerprint(json.loads(s.path.read_bytes()))==fingerprint(s.state)


def test_active_consumer_blocks_migration(store,monkeypatch):
    s=store
    monkeypatch.setattr(storage,'_assert_quiescent',lambda _:storage.fail('native consumer active'))
    with pytest.raises(ContractError,match='consumer active'):migrate(s)
    assert storage.MARKER not in json.loads(s.path.read_bytes())


def test_protected_reads_reuse_once_but_guards_and_full_commit_reads_remain(store):
    s=store;migrate(s);batch=s.service.knowledge._batch_path('batch');data=batch.read_bytes()
    graph={'records':{batch:(data,hashlib.sha256(data).digest())},'directories':{},'bytes':len(data)}
    protector=sealed.Protector(s.service,s.state);protector.prepare(graph);protector.activate()
    try:
        with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=protector.directory,restrict=False) as session:
            cache=VerifiedJsonReads()
            for _ in range(4):assert cache.read(s.path)==s.state
            assert session.stats['first_reads']==2 and session.stats['hits']==6
            assert session.stats['guards']==1+8
            rows=[r for r in ledger.rows.values() if r['origin']=='workflow_storage.segment']
            assert sum(r['reads'] for r in rows)==2
            with sealed.full_evidence_reads():assert cache.read(s.path)==s.state
            rows=[r for r in ledger.rows.values() if r['origin']=='workflow_storage.segment']
            assert sum(r['reads'] for r in rows)==4
            protector.revoke('isolated revocation')
            with pytest.raises(ContractError,match='EVIDENCE_SNAPSHOT_REVOKED'):cache.read(s.path)
    finally:
        if protector.state['state']!='revoked':protector.revoke('isolated cleanup')
