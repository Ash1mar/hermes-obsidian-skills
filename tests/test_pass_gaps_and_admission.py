"""Isolated partial coverage, guarded staging, cleanup and mechanical normalization."""
import copy,hashlib,json,subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_compact_execution import stopped,vault,sync
from test_p3_knowledge_build import workflow_request
from ingest_kanban import desired_graph,mechanical_failure_codes
from hermes_source_units import ContractError
from hermes_source_units.exact_duplicates import normalize_pass
from hermes_source_units.pass_skips import skippable_failure
from test_pass_refill_projection import projection

def prepare_skip(stopped,monkeypatch,code='SEMANTIC_REVIEW_REQUIRED'):
    a,w,_=stopped
    monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[])
    monkeypatch.setattr(a.kanban,'task_states',lambda _:{t:a.kanban.task_snapshot('',t)['task']['status'] for t in a.kanban.by_id},raising=False)
    entries=[]
    for card in w['kanban']['task_map']:
        sid=card['node'].partition(':')[2]
        if a.workflow.knowledge._slice(w['batch_id'],sid)['state']=='completed':continue
        ref=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},code,'Unverified source or semantic evidence')
        entries.append({'slice_id':sid,'reason':'Retain an explicit unresolved evidence gap; no unchanged retry',
            'evidence':[{'ref':ref,'sha256':hashlib.sha256((a.workflow.vault/ref).read_bytes()).hexdigest()}]})
    request=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        operation_id='skip-unverified',entries=entries)
    return a,w,request

def test_partial_pause_pins_gaps_and_reduce_excludes_them(stopped,vault,monkeypatch):
    a,w,request=prepare_skip(stopped,monkeypatch)
    oldpasses={p:p.read_bytes() for p in vault.glob('_system/knowledge-builds/task-*/passes/*.json')}
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    assert not a.skip_pass_slices(request,dry_run=True)['applied']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    r=a.skip_pass_slices(request);assert r['applied'] and a.skip_pass_slices(request)['replayed']
    current=a.workflow.status(w['workflow_id'])
    excluded=a.workflow.skipped_pass_tasks(current)
    assert excluded and all(a.workflow.knowledge._task(t)['status']=='skipped' for t in excluded)
    assert all(p.read_bytes()==b for p,b in oldpasses.items())
    resumed=a.resume(workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    assert resumed['pause']['boundary']=='pass' and not resumed['background_dispatch']
    report=json.loads((vault/current['pause_control']['report_ref']).read_text())
    assert report['evidence']['pass_outcome']=='partial' and report['evidence']['pass_coverage_gaps']
    with pytest.raises(ContractError,match='WORKFLOW_PAUSED'):
        a.workflow.reconcile(workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=current['revision'],target_stage='reducing'))
    continuation=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=current['revision'],
        continue_id='continue-partial',boundary='pass',evidence_digest=current['pause_control']['evidence_digest'])
    continued=a.workflow.continue_workflow(continuation)
    reduced=a.workflow.reconcile(workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=continued['revision'],target_stage='reducing'))
    nodes=desired_graph(a.workflow,reduced)
    skipped_nodes={'pass-slice:'+s for s in a.workflow.skipped_pass_slices(reduced)}
    assert not any(skipped_nodes.intersection(n.parents) for n in nodes if n.kind=='resource-reduce')
    # Reducer input must omit every excluded task's candidates and prior Passes.
    import fixed_reduce
    node=next(n for n in nodes if n.kind=='resource-reduce')
    binding={'workflow_id':w['workflow_id'],'node':node.name,'task_id':'isolated-reducer'}
    monkeypatch.setattr(a,'worker_check',lambda _:{'batch_id':w['batch_id']})
    snapshot=fixed_reduce.snapshot(a,binding)
    assert excluded.isdisjoint(t['task_id'] for t in snapshot['backend']['tasks'])
    assert snapshot['view']['coverage_limitations']['excluded_task_count']==len(excluded)
    pointer=current['pass_skip_history'][0]
    (vault/pointer['report_ref']).write_text('{}')
    with pytest.raises(ContractError,match='STALE_INPUT'):a.workflow.verify_pass_skips(reduced)

def test_interrupted_skip_requires_exact_request_and_keeps_outputs(stopped,vault,monkeypatch):
    a,w,request=prepare_skip(stopped,monkeypatch)
    write=a.workflow._write
    monkeypatch.setattr(a.workflow,'_write',lambda _:(_ for _ in ()).throw(OSError('isolated interruption')))
    with pytest.raises(OSError):a.skip_pass_slices(request)
    assert a.workflow._pass_skip_journal(w['workflow_id']).exists()
    with pytest.raises(ContractError,match='PASS_SKIP_PENDING'):a.workflow.assert_execution_ready(a.workflow.status(w['workflow_id']))
    monkeypatch.setattr(a.workflow,'_write',write)
    assert a.skip_pass_slices(request)['applied']
    assert not a.workflow._pass_skip_journal(w['workflow_id']).exists()
    assert a.skip_pass_slices(request)['replayed']

@pytest.mark.parametrize('code',['READING_WINDOW_OVERSIZE','WORKFLOW_STOPPED','LOCK_TIMEOUT','ACCESS_DENIED'])
def test_program_failures_cannot_be_skipped(stopped,monkeypatch,code):
    a,w,r=prepare_skip(stopped,monkeypatch,code)
    with pytest.raises(ContractError,match='SKIP_NOT_ALLOWED'):a.skip_pass_slices(r)
    assert not a.workflow._pass_skip_journal(w['workflow_id']).exists()

def test_duplicate_normalization_preserves_unequal_semantics():
    ref={'unit_ref':{'unit_id':'u'},'spans':[{'start':0,'end':1}]}
    c={'candidate_id':'x','finding':'A AND B','conditions':['A','B'],'support_refs':[ref,copy.deepcopy(ref)]}
    draft={'candidates':[c,copy.deepcopy(c),{**c,'finding':'A OR B'}],
        'inspections':[{'source_ref':ref,'qa':'usable'},{'source_ref':ref,'qa':'uncertain'}]}
    cleaned,n=normalize_pass(draft)
    assert n==4 and len(cleaned['candidates'])==2 and len(cleaned['inspections'])==2
    assert {c['finding'] for c in cleaned['candidates']}=={'A AND B','A OR B'}
    assert len(draft['candidates'])==3

def test_native_busy_error_remains_retryable_contract(monkeypatch):
    import orchestration
    monkeypatch.setattr(orchestration.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],1,
        '{"ok":false,"error":"LOCK_BUSY at $: current atomic commit"}',''))
    with pytest.raises(ContractError,match='LOCK_BUSY'):orchestration.dispatch_ready_programs('/isolated','ingest-test')

def test_large_program_packet_splits_intact_before_model():
    from test_phase_grouping_dependencies import grouping_fixture
    import fixed_semantic as f
    from hermes_source_units.semantic_budget import PHASE_BALANCED_POLICY
    packet,tasks=grouping_fixture(8);before=copy.deepcopy(packet)
    assert len(json.dumps(packet,ensure_ascii=False))>30000
    groups,plan=f.build_group_plan(packet,tasks,'candidate',30000,{},PHASE_BALANCED_POLICY,pinned=True)
    assert plan['state']=='ready' and len(groups)>1
    assert [t['task'] for g in groups for t in g]==[t['task'] for t in tasks]
    assert all(not r['blocking_reasons'] for r in plan['groups']) and packet==before

def test_public_snapshot_cannot_hide_live_worker_and_missing_receipt_can_settle_exact_run(projection,monkeypatch):
    import ingest_kanban as module
    from datetime import datetime
    p=projection;item=p.slices['s2']
    item['revision']=5;item['lease']['claimed_at']='2026-10-14T00:00:00.500000Z'
    claim=datetime.fromisoformat(item['lease']['claimed_at'].replace('Z','+00:00')).timestamp()
    p.native['t2']['status']='ready'
    p.snapshots['t2']['task']={k:v for k,v in p.native['t2'].items() if k not in ('worker_pid','current_run_id','claim_lock','idempotency_key')}
    p.snapshots['t2']['runs']=[{'id':2,'profile':module.PROGRAM_ASSIGNEE,'started_at':int(claim)-1,'ended_at':int(claim)}]
    monkeypatch.setattr(p.adapter.kanban,'current_task_grant',lambda _,tid:copy.deepcopy(p.native[tid]),raising=False)
    calls=[];p.failed.add('pass-slice:s2')
    p.adapter.workflow.knowledge.slice_fail=lambda request:calls.append(request)
    module.IngestKanbanAdapter._settle_ended_pass_leases(p.adapter,p.value)
    assert not calls  # Private current PID is live despite its omission by show JSON.
    p.native['t2']['worker_pid']=None
    module.IngestKanbanAdapter._settle_ended_pass_leases(p.adapter,p.value)
    assert len(calls)==1 and calls[0]['worker_id']=='ingest-worker-t2' and calls[0]['expected_revision']==5
