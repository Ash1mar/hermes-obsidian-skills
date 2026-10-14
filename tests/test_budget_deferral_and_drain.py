"""Isolated operator deferral and abnormal shutdown; no real worker outputs."""
import copy,importlib.util,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_source_units import ContractError,mutation_digest
from test_compact_execution import stopped,vault,sync
from test_pass_refill_projection import projection
from test_pass_gaps_and_admission import prepare_skip
import ingest_kanban as module

def test_budget_deferral_requires_explicit_scope_and_verifier(stopped,monkeypatch):
    a,w,r=prepare_skip(stopped,monkeypatch,'READING_WINDOW_OVERSIZE')
    with pytest.raises(ContractError,match='SKIP_NOT_ALLOWED'):a.skip_pass_slices(r,dry_run=True)
    for e in r['entries']:e['deferral']='bounded_input'
    r['input_digest']=mutation_digest(r)
    with pytest.raises(ContractError,match='SKIP_NOT_ALLOWED'):
        a.workflow.skip_pass_slices(r,dry_run=True)

def test_budget_deferral_pins_verified_gap_and_preserves_partial_records(stopped,vault,monkeypatch):
    a,w,r=prepare_skip(stopped,monkeypatch,'READING_WINDOW_OVERSIZE')
    for e in r['entries']:e['deferral']='bounded_input'
    r['input_digest']=mutation_digest(r)
    proof={'phase':'citation','packet_sha256':'fixture-packet-hash','verification_success':False,
           'plan':{'state':'blocked','input_limit_codepoints':30000,'groups':[{'input_codepoints':32876}]}}
    monkeypatch.setattr(a,'_citation_budget_deferral',lambda *args:copy.deepcopy(proof))
    before={p:p.read_bytes() for p in vault.glob('_system/knowledge-builds/task-*/passes/*.json')}
    preview=a.skip_pass_slices(r,dry_run=True)
    assert all(e['evidence'][0]['category']=='CITATION_BUDGET_DEFERRED' for e in preview['coverage_gaps'])
    out=a.skip_pass_slices(r)
    report=json.loads((vault/out['report_ref']).read_text())
    assert not report['verification_success'] and report['entries'][0]['evidence'][0]['budget_proof']==proof
    assert all(p.read_bytes()==b for p,b in before.items()) and a.skip_pass_slices(r)['replayed']

def test_settlement_uses_actual_process_liveness_with_stale_private_pid(projection,monkeypatch):
    from datetime import datetime
    p=projection;s=p.slices['s2'];s['revision']=5;s['lease']['claimed_at']='2026-10-14T00:00:00.500000Z'
    stamp=datetime.fromisoformat(s['lease']['claimed_at'].replace('Z','+00:00')).timestamp()
    p.native['t2']['status']='archived'
    p.snapshots['t2']['runs']=[{'id':2,'profile':module.PROGRAM_ASSIGNEE,'started_at':stamp-1,'ended_at':stamp+1}]
    monkeypatch.setattr(p.adapter.kanban,'current_task_grant',lambda _,tid:copy.deepcopy(p.native[tid]),raising=False)
    p.failed.add('pass-slice:s2');calls=[]
    p.adapter.workflow.knowledge.slice_fail=lambda r:calls.append(r)
    monkeypatch.setattr(p.adapter.kanban,'worker_is_active',lambda *a:True,raising=False)
    p.native['t2']['worker_pid']=None
    module.IngestKanbanAdapter._settle_ended_pass_leases(p.adapter,p.value)
    assert not calls  # Real activity wins even when the recorded PID is absent.
    p.native['t2']['worker_pid']=102
    monkeypatch.setattr(p.adapter.kanban,'worker_is_active',lambda *a:False)
    module.IngestKanbanAdapter._settle_ended_pass_leases(p.adapter,p.value)
    assert len(calls)==1 and calls[0]['expected_revision']==5 and calls[0]['worker_id']=='ingest-worker-t2'

def test_blocked_watcher_drains_before_exit_and_bounds_unverified_cleanup(monkeypatch):
    path=Path(module.__file__).parents[1]/'scripts/watch_ingest_workflow.py'
    spec=importlib.util.spec_from_file_location('isolated_watcher',path);watch=importlib.util.module_from_spec(spec);spec.loader.exec_module(watch)
    owned={'lease':{'worker_id':'isolated'}};events=[]
    value={'cancel_requested':False,'batch_id':'isolated'}
    a=SimpleNamespace(workflow=SimpleNamespace(status=lambda _:value,knowledge=SimpleNamespace(_slices=lambda _:[owned])))
    a.reconcile_pass_workers=lambda _: (events.append('settle'),owned['lease'].update(worker_id=None))
    wake=SimpleNamespace(wait=lambda _:None,clear=lambda:None)
    watch.settle_blocked_pass(a,'isolated',wake,3)
    assert events==['settle']
    owned['lease']['worker_id']='isolated';a.reconcile_pass_workers=lambda _:events.append('still-owned')
    stamps=iter([0,31]);monkeypatch.setattr(watch.time,'monotonic',lambda:next(stamps))
    with pytest.raises(ContractError,match='PASS_SETTLEMENT_PENDING'):
        watch.settle_blocked_pass(a,'isolated',wake,3)
    assert owned['lease']['worker_id']=='isolated'
