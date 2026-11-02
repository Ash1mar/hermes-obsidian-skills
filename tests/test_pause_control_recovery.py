"""Control-only pause recovery with real file-backed evidence; isolated faults."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from test_build_policy_adoption import held,vault,files
from hermes_source_units import mutation_digest,ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _json_bytes,_load_json
from hermes_source_units.partial_finalize import pin
from pause_control_recovery import recover
from native_stop_inventory import settle_workflow

@pytest.fixture
def paused(held,monkeypatch):
    a,w,_=held;s=a.workflow;k=s.knowledge
    batch=k._batch(w['batch_id']);batch.update(state='analyzing',last_operation='slice')
    k._batch_path(w['batch_id']).write_bytes(_json_bytes(batch))
    # Recreate this isolated fixture's native pause after its setup transition.
    (s.vault/w['pause_control']['report_ref']).unlink()
    w['pause_control'].update(boundary=None,evidence_digest=None,report_ref=None);s._write(w)
    q={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision']};q['input_digest']=mutation_digest(q)
    w=s.latch_pause(q)
    monkeypatch.setattr(a.kanban,'task_states',lambda _: {},raising=False)
    monkeypatch.setattr('hermes_source_units.runtime_telemetry.notify_reconciler',lambda *a:False)
    return a,w

@pytest.fixture
def cancelled(paused):
    a,w=paused;s=a.workflow;k=s.knowledge;batch=k._batch(w['batch_id']);old=copy.deepcopy(w)
    root=s.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/operator-operations"
    def receipt(action,req,status):
        req={**req,'input_digest':mutation_digest(req)};d=root/action;d.mkdir(parents=True)
        (d/'request.json').write_bytes(_json_bytes({'intent':{'workflow_id':w['workflow_id'],'action':action,'operation_id':action},'request':req}))
        (d/'result.json').write_bytes(_json_bytes({'ok':True,'action':action,'operation_id':action,
            'request_ref':(d/'request.json').relative_to(s.vault).as_posix(),'status':status}))
    status={'workflow_id':w['workflow_id'],'batch_id':w['batch_id'],'batch_revision':batch['revision'],
        'batch_state':'analyzing','revision':w['revision']-1}
    receipt('resume',{'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision']-2},status)
    req={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision']};req['input_digest']=mutation_digest(req)
    cw=s.cancel(req);k.cancel_batch(batch['batch_id'],w['actor'],batch['revision'])
    receipt('cancel',req,{**status,'revision':cw['revision'],'batch_revision':batch['revision']+1,'batch_state':'cancelled',
        'pause':{k:w['pause_control'][k] for k in ('report_ref','evidence_digest')}})
    request={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':cw['revision'],
        'operation_id':'recover-pause','action':'recover-paused-control','reason':'Explicit isolated recovery',
        'pause_report':pin(s.vault,w['pause_control']['report_ref'])}
    request['input_digest']=mutation_digest(request)
    return a,old,request

def test_normal_pause_cleanup_is_repeatable_without_cancel(paused,monkeypatch):
    a,w=paused;before=files(a.workflow.vault)
    monkeypatch.setattr('native_ingest.control_workflow',lambda *a:pytest.fail('must not cancel valid pause'))
    assert settle_workflow(a,w['workflow_id'],'stop')['paused']
    assert settle_workflow(a,w['workflow_id'],'stop')['paused']
    assert before==files(a.workflow.vault)

def test_authenticated_recovery_and_replay_preserve_original_and_boundary(cancelled):
    a,old,q=cancelled;s=a.workflow;original=(s.vault/q['pause_report']['ref']).read_bytes();before=files(s.vault)
    assert recover(a,q,dry_run=True)['dry_run'];assert before==files(s.vault)
    result=recover(a,q);w=s.status(q['workflow_id'])
    assert not w['cancel_requested'] and w['pause_control']['boundary']==old['pause_control']['boundary']
    assert w['pause_control']['history']==old['pause_control']['history'] and result['new_domain_calls']==0
    s._verify_pause_snapshot(w);after=files(s.vault)
    assert recover(a,q)['replayed'] and after==files(s.vault)
    assert (s.vault/q['pause_report']['ref']).read_bytes()==original

@pytest.mark.parametrize('change',['content','scope','revision','actor','report','control'])
def test_changed_evidence_or_authority_denies_recovery_without_writes(cancelled,change):
    a,old,q=cancelled;s=a.workflow
    if change=='content':
        path=s.knowledge._run_path(s.knowledge._batch(old['batch_id'])['run_ids'][0]).parent/'pages'
        next(path.glob('*.md')).write_text('changed original domain page')
    elif change=='scope':
        w=s.status(q['workflow_id']);w['scope']['provider']='sync';s._write(w)
    elif change=='revision':q['expected_revision']-=1
    elif change=='actor':q['actor']='other'
    elif change=='report':(s.vault/q['pause_report']['ref']).write_text('{}')
    elif change=='control':
        root=s.vault/f"_system/ledgers/ingest-workflows/{q['workflow_id']}/operator-operations/cancel/result.json"
        r=_load_json(root);r['status']['batch_revision']-=1;root.write_bytes(_json_bytes(r))
    q['input_digest']=mutation_digest(q);before=files(s.vault)
    with pytest.raises(ContractError):recover(a,q)
    assert before==files(s.vault)

def test_interrupted_control_write_finishes_exact_forward_transition(cancelled,monkeypatch):
    a,old,q=cancelled;s=a.workflow;write=s._write
    def crash(*a):raise RuntimeError('isolated after batch write')
    monkeypatch.setattr(s,'_write',crash)
    with pytest.raises(RuntimeError):recover(a,q)
    monkeypatch.setattr(s,'_write',write)
    assert recover(a,q)['ok'];s._verify_pause_snapshot(s.status(q['workflow_id']))
