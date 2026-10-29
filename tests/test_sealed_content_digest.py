"""A reusable digest never substitutes for a current evidence guard."""
import hashlib,json,os
import pytest
from test_sealed_evidence_snapshot import snapshot
from hermes_source_units import ContractError,evidence_snapshot as sealed
from hermes_source_units.read_accounting import accounting_scope,read_hashed
from hermes_source_units.read_reuse_policy import reference_reads
from hermes_source_units.verified_json import VerifiedJsonReads


def test_digest_calculations_follow_content_while_every_guard_remains(snapshot):
    s=snapshot;path=s.vault/'inputs/task.json'
    with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as session:
        before=session.stats['guards'];checks=session.stats['metadata_checks']
        cache=VerifiedJsonReads()
        for _ in range(4):assert cache.read(path,origin='digest-fixture')=={'value':1}
        assert session.stats['guards']-before==4
        assert session.stats['metadata_checks']>checks
        rows=[r for r in ledger.hash_rows.values() if r['path']==str(path)]
        assert sum(r['hash_calls'] for r in rows)==1 and sum(r['digest_reuses'] for r in rows)==4
        with reference_reads(hashes=False):
            for _ in range(3):cache.read(path,origin='digest-fixture')
        assert session.stats['guards']-before==7
        assert sum(r['hash_calls'] for r in ledger.hash_rows.values() if r['path']==str(path))==4
        with pytest.raises(ContractError,match='SOURCE_CHANGED'):
            cache.read(path,origin='digest-fixture',expected='0'*64)
        assert session.stats['guards']-before==8
        with sealed.full_evidence_reads():cache.read(path,origin='full-commit')
        assert sum(r['hash_calls'] for r in ledger.hash_rows.values() if r['path']==str(path))==5


def test_unprotected_current_bytes_and_reference_mode_are_never_digest_cached(tmp_path):
    path=tmp_path/'plain.json';path.write_bytes(b'{"value":1}')
    with accounting_scope() as ledger:
        for _ in range(3):assert read_hashed(path,'plain')[1]==hashlib.sha256(path.read_bytes()).hexdigest()
        stamp=path.stat().st_mtime_ns;path.write_bytes(b'{"value":2}');os.utime(path,ns=(stamp,stamp))
        assert json.loads(read_hashed(path,'plain')[0])=={'value':2}
        rows=list(ledger.hash_rows.values());assert sum(r['hash_calls'] for r in rows)==4
        assert sum(r['digest_reuses'] for r in rows)==0


def test_reusable_digest_cannot_hide_metadata_drift(snapshot):
    s=snapshot;path=s.vault/'inputs/task.json'
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        read_hashed(path,'first')
        bits=sealed.flags(path);sealed.flags(path,bits&~sealed.IMMUTABLE)
        path.write_bytes(b'{"value":2}');sealed.flags(path,bits)
        with pytest.raises(ContractError,match='metadata changed'):read_hashed(path,'changed')
