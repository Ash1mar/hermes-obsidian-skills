"""Isolated acknowledgement races; no real Vault or domain retry is executed."""
import copy,json,os,threading
from contextlib import nullcontext
from types import SimpleNamespace as NS
import pytest

from test_refill_handoff import script_module,no_span,refill
import terminal_completion as terminal
import native_program_dispatch as dispatcher
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units import ContractError


@pytest.fixture
def completion(tmp_path,monkeypatch):
    vault=tmp_path/'vault';vault.mkdir();proof=vault/'valid-qa-result.json';proof.write_text('{"qa":"needs-qa","max_extra_checks":1}')
    root=tmp_path/'host-runtime';root.mkdir()
    s=NS(events=[],clock=0.0,syncs=0,pending=True,linger=False,transient=None,permanent=None)
    w={'workflow_id':'isolated-final','actor':'agent','state':'partial','cancel_requested':False,
       'revision':12,'batch_id':'batch','current_stage':'validating','release_id':'release',
       'kanban':{'board_id':'board','task_map':[]}}
    def sync(wid,**kw):
        s.syncs+=1;s.events.append('sync')
        if s.transient and s.syncs==1:raise ContractError(s.transient,'$','transient native contention')
        if s.permanent:raise ContractError(s.permanent,'$','invalid evidence')
        if not s.linger:s.pending=False
        return {'background_dispatch':False,'program_dispatch':False}
    s.adapter=NS(workflow=NS(vault=vault,status=lambda _:copy.deepcopy(w),knowledge=NS(_slices=lambda _:[])),
        kanban=NS(task_states=lambda _:{'acceptance':'running' if s.pending else 'done'}),_sync_current=sync)
    s.w=w;s.root=root;s.proof=proof
    class Wake:
        def clear(self):s.events.append('clear')
        def wait(self,delay):s.events.append('wait');s.clock+=delay
    s.wake=Wake()
    monkeypatch.setattr(terminal,'runtime_directory',lambda *a:root)
    monkeypatch.setattr(terminal,'span',no_span)
    monkeypatch.setattr(terminal,'time',NS(monotonic=lambda:s.clock))
    monkeypatch.setattr(terminal,'workflow_processes',lambda *a:[{'pid':os.getpid(),'kind':'reconciler'}])
    monkeypatch.setattr(terminal,'final_evidence',lambda *a:s.events.append('validate'))
    return s


@pytest.mark.parametrize('state',['completed','partial'])
def test_real_watcher_reconciles_terminal_before_exit_without_dispatch(completion,monkeypatch,state):
    s=completion;s.w['state']=state;watcher=script_module('watch_ingest_workflow')
    monkeypatch.setattr(watcher,'IngestKanbanAdapter',lambda *a,**kw:s.adapter)
    monkeypatch.setattr(watcher,'_exclusive_lock',lambda *a:nullcontext())
    monkeypatch.setattr(watcher,'wake_events',lambda *a:nullcontext(s.wake))
    monkeypatch.setattr(watcher,'dispatch_ready_programs',lambda *a:pytest.fail('terminal must not dispatch'))
    before=s.proof.read_bytes();watcher.watch(str(s.adapter.workflow.vault),s.w['workflow_id'])
    report=json.loads((s.root/'terminal-completion.json').read_text())
    assert report['state']=='settled' and report['domain_calls_granted']==0 and not report['domain_rerun']
    assert s.events==['clear','validate','sync'] and s.proof.read_bytes()==before


@pytest.mark.parametrize('state,cancel',[('failed',False),('cancelled',True)])
def test_stopped_watcher_never_reconciles_or_releases_pause(completion,monkeypatch,state,cancel):
    s=completion;s.w.update(state=state,cancel_requested=cancel);watcher=script_module('watch_ingest_workflow')
    monkeypatch.setattr(watcher,'IngestKanbanAdapter',lambda *a,**kw:s.adapter)
    monkeypatch.setattr(watcher,'_exclusive_lock',lambda *a:nullcontext())
    monkeypatch.setattr(watcher,'wake_events',lambda *a:nullcontext(s.wake))
    watcher.watch(str(s.adapter.workflow.vault),s.w['workflow_id'])
    assert s.syncs==0 and not (s.root/'terminal-completion.json').exists()


def test_acknowledgement_waits_for_exact_workflow_consumer_drain_and_is_idempotent(completion,monkeypatch):
    s=completion;scans=[]
    def processes(wid,board):
        scans.append((wid,board))
        return ([{'pid':999999,'kind':'native_worker'}] if len(scans)==1 else [])
    monkeypatch.setattr(terminal,'workflow_processes',processes)
    terminal.settle(s.adapter,s.w['workflow_id'],s.wake,1)
    assert s.syncs==2 and 'wait' in s.events and scans==[('isolated-final','board')]*2
    before=s.proof.read_bytes();terminal.settle(s.adapter,s.w['workflow_id'],s.wake,1)
    assert s.proof.read_bytes()==before and json.loads((s.root/'terminal-completion.json').read_text())['state']=='settled'


@pytest.mark.parametrize('code',sorted(terminal.TRANSIENT))
def test_transient_acknowledgement_poll_does_not_grant_domain_retry(completion,code):
    s=completion;s.transient=code;terminal.settle(s.adapter,s.w['workflow_id'],s.wake,1)
    assert s.syncs==2
    report=json.loads((s.root/'terminal-completion.json').read_text())
    assert report['state']=='settled' and report['domain_calls_granted']==0


def test_permanent_contract_error_and_timeout_are_not_reported_as_success(completion):
    s=completion;s.permanent='SOURCE_CHANGED'
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):terminal.settle(s.adapter,s.w['workflow_id'],s.wake,1)
    assert json.loads((s.root/'terminal-completion.json').read_text())['state']=='held'
    s.permanent=None;s.linger=True
    with pytest.raises(ContractError,match='TERMINAL_COMPLETION_PENDING'):
        terminal.settle(s.adapter,s.w['workflow_id'],s.wake,1,timeout=2)
    assert s.clock==2 and json.loads((s.root/'terminal-completion.json').read_text())['domain_rerun'] is False


@pytest.mark.parametrize('state',['completed','partial','failed','cancelled'])
def test_terminal_native_dispatch_and_graph_eligibility_grant_no_claims(refill,state):
    original=refill.adapter.workflow.status
    refill.adapter.workflow.status=lambda wid:{**original(wid),'state':state}
    assert dispatcher.dispatch_programs(refill.adapter,'ingest-refill')==[]
    assert 'reap' not in refill.events and 'fast-reconcile' not in refill.events
    adapter=NS(enable_workers=True,_canary_allows=lambda *a:pytest.fail('no terminal eligibility'))
    assert not IngestKanbanAdapter._eligible(adapter,{'state':state},NS(),[])


@pytest.mark.parametrize('invalid',['missing-final-refs','failed-lint','invalid-release'])
def test_terminal_label_without_valid_final_evidence_never_certifies_completion(tmp_path,monkeypatch,invalid):
    from hermes_source_units.source_units import _json_bytes
    # Use the actual validator, not the mocked completion fixture.
    v=tmp_path/'vault';ref='_system/ledgers/ingest-workflows/w/reports/acceptance-lint.json'
    p=v/ref;p.parent.mkdir(parents=True);p.write_bytes(_json_bytes({'ok':invalid!='failed-lint'}))
    w={'workflow_id':'w','state':'partial','current_stage':'validating','release_id':'r',
       'artifacts':[{'stage':'partial','refs':[ref,'_system/knowledge-releases/r/manifest.json']}]}
    if invalid=='missing-final-refs':w['artifacts']=[]
    a=NS(workflow=NS(vault=v,assert_execution_ready=lambda _:None))
    monkeypatch.setattr(terminal,'FileVaultFinalizeService',lambda _:NS(validate=lambda _:{'ok':invalid!='invalid-release'}))
    with pytest.raises(ContractError,match='TERMINAL_COMPLETION_UNPROVEN'):terminal.final_evidence(a,w)
