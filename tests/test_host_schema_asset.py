"""Host schema contract is explicit; no test changes installed schema files."""
import hashlib,json,multiprocessing,os,time
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import evidence_snapshot as sealed,validation,ContractError
from hermes_source_units import schema_assets
from hermes_source_units.read_accounting import accounting_scope,read_hashed
from hermes_source_units.read_reuse_policy import reference_reads

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux host schema contract')


def fixture_data(tmp_path):
    vault=tmp_path/'vault';vault.mkdir();batch=vault/'batch.json';batch.write_bytes(b'{"task_ids":[]}')
    service=NS(vault=vault,knowledge=NS(_batch_path=lambda _:batch))
    w={'workflow_id':'fixture-host','batch_id':'b','current_stage':'build_finalizing','execution_recovery_history':[]}
    raw=batch.read_bytes();graph={'records':{batch:(raw,hashlib.sha256(raw).digest())},'directories':{},'bytes':len(raw)}
    root=tmp_path/'install';schema=root/'lib/schemas/contracts.json';schema.parent.mkdir(parents=True);schema.write_bytes(b'{"version":1}')
    asset={'contract':schema_assets.CONTRACT,'root':str(root),'path':str(schema)}
    return NS(vault=vault,service=service,w=w,graph=graph,root=root,schema=schema,asset=asset)


@pytest.fixture
def hosted(tmp_path,monkeypatch):
    s=fixture_data(tmp_path);monkeypatch.setattr(validation,'__file__',str(s.schema.parent.parent/'validation.py'))
    s.protector=sealed.Protector(s.service,s.w,schema_asset=s.asset)
    s.protector.prepare(s.graph);s.protector.activate()
    try:yield s
    finally:
        if s.protector.state['state']!='revoked':s.protector.revoke('isolated host schema cleanup')


def test_schema_has_one_first_read_but_every_guard_and_current_digest_comparison_remains(hosted):
    s=hosted
    with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=s.protector.directory,restrict=False) as session:
        before=session.stats['guards']
        for _ in range(4):assert read_hashed(s.schema,'schema-check')[0]==b'{"version":1}'
        assert session.stats['guards']-before==4
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.schema))==1
        with reference_reads(hashes=True,schema=False):
            for _ in range(3):read_hashed(s.schema,'schema-check')
        assert session.stats['guards']-before==7
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.schema))==4
        with sealed.full_evidence_reads():read_hashed(s.schema,'locked-check')
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.schema))==5
        assert str(s.schema) not in session._static_names


def test_schema_same_mtime_rewrite_is_not_hidden_by_a_digest(hosted):
    s=hosted
    with sealed.snapshot_scope(s.service,directory=s.protector.directory,restrict=False):
        read_hashed(s.schema,'first');stamp=s.schema.stat().st_mtime_ns
        bits=sealed.flags(s.schema);sealed.flags(s.schema,bits&~sealed.IMMUTABLE)
        s.schema.write_bytes(b'{"version":2}');os.utime(s.schema,ns=(stamp,stamp));sealed.flags(s.schema,bits)
        with pytest.raises(ContractError,match='metadata changed'):read_hashed(s.schema,'changed')


def test_revoke_restores_host_flags_and_new_generation_verifies_new_version(hosted):
    s=hosted;original=dict(s.protector.original)
    with sealed.snapshot_scope(s.service,directory=s.protector.directory,restrict=False):
        read_hashed(s.schema,'first');s.protector.revoke('controlled schema replacement')
        with pytest.raises(ContractError,match='not a live'):read_hashed(s.schema,'revoked')
    assert all(sealed.flags(Path(p))==bits for p,bits in original.items())
    s.schema.write_bytes(b'{"version":2}')
    p=sealed.Protector(s.service,s.w,schema_asset=s.asset);p.prepare(s.graph);p.activate()
    try:
        s.protector.revoke('duplicate old owner must not unseal the new generation')
        with sealed.snapshot_scope(s.service,directory=p.directory,restrict=False):
            assert read_hashed(s.schema,'new')[1]==hashlib.sha256(b'{"version":2}').hexdigest()
    finally:p.revoke('isolated second generation finished')


def test_schema_inode_has_one_owner_and_stale_seal_is_not_adopted(hosted):
    s=hosted
    with pytest.raises(ContractError,match='another protector'):schema_assets.acquire(s.asset)
    s.protector._release_schema_owner()  # isolated simulation of owner FD loss
    with pytest.raises(ContractError,match='already sealed'):schema_assets.acquire(s.asset)


def test_host_ancestor_replacement_is_detected(hosted):
    s=hosted;other=s.root.with_name('moved-install')
    with sealed.snapshot_scope(s.service,directory=s.protector.directory,restrict=False):
        read_hashed(s.schema,'first');s.root.rename(other);s.root.symlink_to(other,target_is_directory=True)
        try:
            with pytest.raises(ContractError,match='evidence path'):read_hashed(s.schema,'symlinked')
        finally:s.root.unlink();other.rename(s.root)


def serve(s):
    p=sealed.Protector(s.service,s.w,schema_asset=s.asset);p.prepare(s.graph);p.serve()


def test_dead_protector_never_authorizes_cached_schema_and_requires_stale_release(tmp_path,monkeypatch):
    s=fixture_data(tmp_path);monkeypatch.setattr(validation,'__file__',str(s.schema.parent.parent/'validation.py'))
    process=multiprocessing.get_context('fork').Process(target=serve,args=(s,));process.start()
    directory=s.vault/sealed.ROOT/s.w['workflow_id'];state=directory/'state.json'
    until=time.monotonic()+10
    while time.monotonic()<until:
        if state.exists() and json.loads(state.read_text()).get('state')=='active':break
        time.sleep(.02)
    else:pytest.fail('isolated host protector did not activate')
    try:
        with sealed.snapshot_scope(s.service,directory=directory,restrict=False):
            read_hashed(s.schema,'first');process.kill();process.join(5)
            with pytest.raises(ContractError,match='not a live'):read_hashed(s.schema,'dead')
        with pytest.raises(ContractError,match='already sealed'):schema_assets.acquire(s.asset)
    finally:
        if process.is_alive():process.kill();process.join(5)
        manifest=json.loads((directory/'manifest.json').read_text())
        for p in (directory,directory/'state.json',directory/'manifest.json',directory/'lease.lock'):
            sealed.flags(p,sealed.flags(p)&~sealed.IMMUTABLE)
        for name,bits in manifest['original_flags'].items():sealed.flags(Path(name),bits)


def test_host_scope_and_stale_release_are_not_arbitrary_path_authority(hosted,tmp_path):
    s=hosted
    with pytest.raises(ContractError,match='invalid deployment'):schema_assets.validate({**s.asset,'path':str(tmp_path/'unrelated.json')})
    assert not schema_assets.release_allowed(s.vault,s.schema,{'host_schema':s.asset})
