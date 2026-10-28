"""Pure reuse must not remove or merge fresh kernel observations."""
import hashlib,json,os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units import evidence_snapshot as sealed
from hermes_source_units.dependency_views import FrozenOverlay,FrozenProof
from hermes_source_units.read_accounting import accounting_scope
from ingest_graph_reuse import dependencies,derive,graph_scope,checked_dependencies
from test_sealed_evidence_snapshot import snapshot

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux sealed-evidence reader')


def batch_fixture_validation(monkeypatch):
    from hermes_source_units import validation
    real=validation.validate_record
    monkeypatch.setattr(validation,'validate_record',lambda kind,value:None if kind=='knowledge_batch' else real(kind,value))


def legacy_plan(manifest,names):
    # The former observation order: absent paths in input order, then the set of
    # lexical ancestors. This oracle does no filesystem I/O and grants nothing.
    paths=set();absent=[]
    for name in names:
        path=Path(name)
        if name in manifest.get('absent_directories',[]):
            absent.append(path);path=Path(manifest['absent_directory_parents'][name])
        for part in (path,*path.parents):
            if str(part) not in manifest['path_identity']:break
            paths.add(str(part))
    calls=[]
    for name in paths:
        calls.append((name,False))
        if name in manifest['content_identity']:calls.append((name,True))
    return absent,calls


def test_cached_plan_preserves_every_state_flag_and_stat_observation(snapshot,monkeypatch):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        names=view._graph_paths;absent,expected=legacy_plan(view.manifest,names)
        calls=[];states=[];flags=[]
        identity=sealed._identity;flag_reader=sealed.flags;starter=sealed._start
        def checked(path,*,content=False,metrics=None):
            calls.append((str(path),content));return identity(path,content=content,metrics=metrics)
        def current_flags(path,value=None):flags.append(str(path));return flag_reader(path,value)
        def process(pid):states.append(pid);return starter(pid)
        monkeypatch.setattr(sealed,'_identity',checked);monkeypatch.setattr(sealed,'flags',current_flags);monkeypatch.setattr(sealed,'_start',process)
        before=dict(view.costs);count=view.stats['metadata_checks']
        for _ in range(4):
            with view.guard(names):pass
        assert calls==expected*4
        assert len(states)==4 and len(flags)==12
        assert view.costs['lstat_calls']-before['lstat_calls']==len(expected)*4
        assert view.costs['absence_checks']-before['absence_checks']==len(absent)*4
        assert view.stats['metadata_checks']-count==sum(not content for _,content in expected)*4
        assert view.costs['plan_builds']-before['plan_builds']==1
        assert view.costs['plan_hits']-before['plan_hits']==3
        assert view.costs['registry_attempts']-before['registry_attempts']==4


def test_cached_plan_keeps_the_second_content_stat(snapshot,monkeypatch):
    s=snapshot;real=sealed._identity;changed=[]
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        def checked(path,*,content=False,metrics=None):
            value=real(path,content=content,metrics=metrics)
            if path==s.report and not content and not changed:
                changed.append(True);bits=sealed.flags(path);sealed.flags(path,bits&~sealed.IMMUTABLE)
                path.write_text('{}');sealed.flags(path,bits)
            return value
        monkeypatch.setattr(sealed,'_identity',checked)
        with pytest.raises(ContractError,match='metadata changed'):
            with view.guard((str(s.report),)):pass
        assert changed


def test_internal_records_and_proofs_share_only_immutable_data(snapshot,monkeypatch):
    s=snapshot;batch_fixture_validation(monkeypatch)
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view,graph_scope() as scope:
        for _ in range(4):
            assert derive(s.service,s.w,lambda:['same nodes'])==['same nodes']
            proof=checked_dependencies(s.service,s.w)
            assert isinstance(proof,dict) and isinstance(proof['files'],dict)
            assert all(isinstance(value,list) for value in proof['directories'].values())
            proof['files'].clear();next(iter(proof['directories'].values())).append('caller mutation')
        fresh=checked_dependencies(s.service,s.w)
        assert fresh['files'] and all('caller mutation' not in rows for rows in fresh['directories'].values())
        assert view.costs['static_builds']==view.costs['payload_builds']==1
        assert scope['proof_internal_shares']==4 and scope['proof_external_copies']==5
        assert view.costs['external_graph_copies']==0
        one=view.graph(s.w,internal=True);two=view.graph(s.w,internal=True)
        assert isinstance(one['records'],FrozenOverlay) and one['records'].base is two['records'].base
        with pytest.raises(TypeError):one['proof_files']['bad']='bad'
        public=view.graph(s.w);public['records'].clear();public['proof_files'].clear()
        public['directories'].clear()
        assert view.graph(s.w)['records'] and view.graph(s.w)['directories']


def test_first_reads_stay_once_and_bulk_reuse_preserves_logical_hit_count(snapshot,monkeypatch):
    s=snapshot;batch_fixture_validation(monkeypatch)
    with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        for _ in range(4):view.graph(s.w,internal=True)
        assert view.stats['first_reads']==len(view._static_names)
        assert view.stats['hits']==3*len(view._static_names)
        assert view.stats['avoided_bytes']==3*view._static_bytes
        rows=[r for r in ledger.rows.values() if r['origin']=='evidence_snapshot:graph_dependency_scan']
        assert sum(r['captures'] for r in rows)==3*len(view._static_names)
        assert sum(r['bytes'] for r in rows)==0


@pytest.mark.parametrize('batch_name',['00-batch.json','middle/batch.json','zz-batch.json'])
def test_digest_recipe_matches_original_stream_for_every_batch_position(tmp_path,batch_name):
    static={str(tmp_path/'a/file.json'):b'first',str(tmp_path/'z/file.json'):b'second'}
    batch=tmp_path/batch_name
    view=sealed.EvidenceSession.__new__(sealed.EvidenceSession)
    view._paths={};view.costs=dict.fromkeys(('path_objects','static_builds','static_build_ns'),0)
    view.manifest={'files':{p:hashlib.sha256(data).hexdigest() for p,data in static.items()},'reports':{},
        'batch':str(batch),'directories':{},'content_identity':{p:[0,0,0,len(data),0] for p,data in static.items()}}
    view._prepare_static()
    for data in (b'{"task_ids":[]}',b'{ "task_ids": [] }'):
        before=hashlib.sha256()
        records={**{Path(p):hashlib.sha256(raw).digest() for p,raw in static.items()},batch:hashlib.sha256(data).digest()}
        for path in sorted(records):
            name=str(path).encode();before.update(len(name).to_bytes(8,'big'));before.update(name);before.update(records[path])
        after=view._digest_prefix.copy();after.update(view._batch_framing);after.update(records[batch]);after.update(view._digest_suffix)
        assert after.digest()==before.digest()
    assert view.costs['static_builds']==1


@pytest.mark.parametrize('snapshot',[True,'deep'],indirect=True)
def test_missing_directory_appearance_is_not_cached(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        name=view.manifest['absent_directories'][0]
        with view.guard((name,)):pass
        parent=Path(view.manifest['absent_directory_parents'][name]);bits=sealed.flags(parent)
        sealed.flags(parent,bits&~sealed.IMMUTABLE);Path(name).mkdir(parents=True);sealed.flags(parent,bits)
        with pytest.raises(ContractError,match='appeared'):
            with view.guard((name,)):pass
