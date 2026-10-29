"""Bounded report leases preserve observations; no real Vault is modified."""
import json,os,threading
from types import SimpleNamespace as NS
import pytest
from test_sealed_evidence_snapshot import snapshot,wait
from hermes_source_units import ContractError,evidence_snapshot as sealed
from hermes_source_units.read_accounting import read_hashed
from hermes_source_units.read_reuse_policy import reference_reads
import fixed_history
from fixed_semantic_recovery import permitted_tasks

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux protected query contract')


def setup(s):
    pointers=s.w['execution_recovery_history']*3
    s.w['execution_recovery_history']=pointers;s.workflow.write_text(json.dumps(s.w))
    return NS(workflow=s.service),{'workflow_id':'fixture','node':'build-finalize','task_id':'new'},pointers


def test_shared_steps_keep_member_guards_metadata_liveness_and_query_results(snapshot):
    s=snapshot;a,b,pointers=setup(s);rows=[]
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as session:
        for enabled in (False,True):
            before=dict(session.costs)
            with reference_reads(queries=enabled):
                journal=list(fixed_history.journal_rows(a,b,'j','output_seed',reverse=True))
                permitted=permitted_tasks(a,b,compact=True)
            rows.append((journal,permitted,{k:v-before[k] for k,v in session.costs.items()}))
        assert rows[0][:2]==rows[1][:2]
        assert [r['value'] for _,r in rows[1][0]]==['first','second']*3
        assert rows[1][1]['old']['ended_run_ids']==['2']
        for key in ('guard_calls','process_checks','metadata_calls','lstat_calls'):
            assert rows[0][2][key]==rows[1][2][key]
        assert rows[1][2]['registry_attempts']<rows[0][2]['registry_attempts']
        rows[1][0][0][1]['value']='caller modification'
        assert next(fixed_history.journal_rows(a,b,'j','output_seed'))[1]['value']=='first'


def test_revoke_waits_for_pure_step_but_controls_remain_fresh(snapshot):
    s=snapshot;a,b,pointers=setup(s);done=threading.Event();requested=threading.Event();errors=[]
    def revoke():
        requested.set()
        try:sealed.rpc(json.loads((s.directory/'state.json').read_text())['socket'],{'operation':'revoke'})
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        with sealed.history_query(s.service,pointers):
            original=read_hashed(s.report,'first')[0]
            thread=threading.Thread(target=revoke);thread.start();assert requested.wait(2)
            assert not done.wait(.08)
            assert read_hashed(s.report,'next')[0]==original
            s.workflow.write_text(json.dumps({**s.w,'cancel_requested':True}))
            assert fixed_history.history_state(s.service,'fixture')['cancel_requested'] is True
        assert done.wait(3);thread.join();assert not errors
        with pytest.raises(ContractError):read_hashed(s.report,'after acknowledged revoke')
    assert s.report.read_bytes()==original


def test_no_lease_spans_generator_yield_and_captured_rows_keep_lazy_semantics(snapshot):
    s=snapshot;a,b,pointers=setup(s)
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        iterator=fixed_history.journal_rows(a,b,'j','output_seed')
        assert next(iterator)[1]['value']=='first'
        sealed.rpc(json.loads((s.directory/'state.json').read_text())['socket'],{'operation':'revoke'})
        assert next(iterator)[1]['value']=='second'
        with pytest.raises(ContractError):next(iterator)


def test_protector_death_is_checked_at_every_original_member(snapshot):
    s=snapshot;a,b,pointers=setup(s)
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        with sealed.history_query(s.service,pointers):
            read_hashed(s.report,'first');s.process.kill();s.process.join(3)
            with pytest.raises(ContractError,match='not a live'):read_hashed(s.report,'next')


def test_malformed_later_pointer_and_callback_do_not_expand_shared_scope(snapshot):
    s=snapshot;a,b,pointers=setup(s);pointers.append({'report_ref':'../outside','report_sha256':'0'*64})
    s.workflow.write_text(json.dumps(s.w))
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as session:
        assert next(fixed_history.journal_rows(a,b,'j','output_seed'))[1]['value']=='first'
        with pytest.raises(ContractError):list(fixed_history.journal_rows(a,b,'j','output_seed'))
        assert session.costs['query_members']==0
        seen=[]
        def selected(entry):
            assert sealed._QUERY.get() is None
            seen.append(entry);return entry
        assert next(fixed_history.entries(a,b,select=selected))[1]['previous_task_id']=='old'
        assert seen


def test_path_changes_and_body_exceptions_are_not_hidden_by_shared_registry(snapshot):
    s=snapshot;a,b,pointers=setup(s)
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        with pytest.raises(KeyError,match='original error'):
            with sealed.history_query(s.service,pointers):raise KeyError('original error')
        with sealed.history_query(s.service,pointers):
            read_hashed(s.report,'first');bits=sealed.flags(s.report)
            sealed.flags(s.report,bits&~sealed.IMMUTABLE)
            s.report.write_text('{}');sealed.flags(s.report,bits)
            with pytest.raises(ContractError,match='metadata changed'):read_hashed(s.report,'next')
