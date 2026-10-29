"""Generic sealed-content broker, real dropped privileges, isolated Vault only."""
import json,os
from pathlib import Path
import pytest
from test_sealed_evidence_snapshot import snapshot
from hermes_source_units import ContractError,evidence_snapshot as sealed
from hermes_source_units.protected_reader import coordinator_reads,fresh_content_reads
from hermes_source_units.read_accounting import accounting_scope,read_hashed
from hermes_source_units.verified_json import VerifiedJsonReads

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux capability-free broker')


def test_generic_roles_reuse_bytes_with_every_guard_and_unchanged_parent_privileges(snapshot):
    s=snapshot;before=Path('/proc/self/status').read_text();caps=[r for r in before.splitlines() if r.startswith('Cap')]
    with accounting_scope() as ledger:
        with coordinator_reads(s.service,'fixture',roles=('reports',)) as client:
            pid=client.pid;cache=VerifiedJsonReads();original=cache.read(s.report)
            for _ in range(4):assert cache.read(s.report)==original
            changed=cache.read(s.report);changed['entries'].clear();assert cache.read(s.report)==original
            status=Path('/proc',str(pid),'status').read_text()
            for key in ('CapEff','CapPrm','CapBnd','CapAmb'):assert int(next(r.split(':')[1] for r in status.splitlines() if r.startswith(key+':')),16)==0
            assert 'NoNewPrivs:\t1' in status
            assert client.stats['first_payloads']==1 and client.stats['requests']==7
        assert not Path('/proc',str(pid)).exists()
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.report))==1
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.directory/'state.json'))>=8
        assert sum(r['hash_calls'] for r in ledger.hash_rows.values() if r['path']==str(s.report))==1
    assert [r for r in Path('/proc/self/status').read_text().splitlines() if r.startswith('Cap')]==caps


def test_unselected_files_and_mutation_bypass_remain_fresh(snapshot):
    s=snapshot;plain=s.vault/'dynamic.json';plain.write_text('{"cancel":false}')
    with accounting_scope() as ledger,coordinator_reads(s.service,'fixture',roles=('reports',)):
        read_hashed(s.report,'first')
        with fresh_content_reads():read_hashed(s.report,'mutation full read')
        for _ in range(2):read_hashed(plain,'dynamic')
        stamp=plain.stat().st_mtime_ns;plain.write_text('{"cancel":true}');os.utime(plain,ns=(stamp,stamp))
        assert json.loads(read_hashed(plain,'dynamic')[0])['cancel']
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.report))==2
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(plain))==3


def test_revoke_is_not_hidden_and_no_reader_lease_spans_caller_work(snapshot):
    s=snapshot
    with coordinator_reads(s.service,'fixture',roles=('reports',)) as client:
        read_hashed(s.report,'first')
        sealed.rpc(json.loads((s.directory/'state.json').read_text())['socket'],{'operation':'revoke'})
        with pytest.raises(ContractError,match='live sealed'):read_hashed(s.report,'revoked')


def test_protector_and_reader_exit_fail_closed_without_raw_fallback(snapshot):
    s=snapshot
    with coordinator_reads(s.service,'fixture',roles=('reports',)) as client:
        read_hashed(s.report,'first');s.process.kill();s.process.join(3)
        with pytest.raises(ContractError,match='live sealed'):read_hashed(s.report,'dead protector')
        client.process.kill();client.process.wait(timeout=3)
        with pytest.raises(ContractError,match='reader'):read_hashed(s.report,'dead reader')


def test_same_timestamp_path_replacement_still_fails_at_next_use(snapshot):
    s=snapshot
    with coordinator_reads(s.service,'fixture',roles=('reports',)):
        read_hashed(s.report,'first');stamp=s.report.stat().st_mtime_ns
        bits=sealed.flags(s.report);sealed.flags(s.report,bits&~sealed.IMMUTABLE)
        s.report.write_text('{}');os.utime(s.report,ns=(stamp,stamp));sealed.flags(s.report,bits)
        with pytest.raises(ContractError,match='metadata changed'):read_hashed(s.report,'rewritten')


def test_referenced_ancestor_symlink_is_checked_even_when_payload_is_cached(snapshot):
    s=snapshot;parent=s.report.parent;moved=parent.with_name('moved-recovery')
    with coordinator_reads(s.service,'fixture',roles=('reports',)):
        read_hashed(s.report,'first');parent.rename(moved);parent.symlink_to(moved,target_is_directory=True)
        try:
            with pytest.raises(ContractError,match='evidence path'):read_hashed(s.report,'symlinked')
        finally:parent.unlink();moved.rename(parent)


def test_unrelated_exception_and_disabled_reference_scope_do_not_start_a_reader(snapshot):
    s=snapshot
    with pytest.raises(KeyError,match='original error'):
        with coordinator_reads(s.service,'fixture',enabled=False):raise KeyError('original error')
    with coordinator_reads(s.service,'fixture',roles=('reports',),enabled=False) as client:assert client is None
