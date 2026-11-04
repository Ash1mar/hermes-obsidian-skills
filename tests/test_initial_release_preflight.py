"""Preflight admission uses real isolated Build/source evidence, no failed worker."""
import copy
from contextlib import nullcontext
from types import SimpleNamespace as NS
import pytest
from test_initial_release import initial,scoped,ready,vault,writer,material_quality,save,grant_request
from hermes_source_units import ContractError
from hermes_source_units.partial_finalize import pin
from hermes_source_units.initial_release import needs_preflight,admitted_pointer
from hermes_source_units.recovery_policy import DEFAULT
from hermes_source_units.validation import fingerprint
import release_recovery
import ingest_kanban


@pytest.mark.parametrize('runs',[[],[{'ended_at':1,'status':'scheduled','outcome':'scheduled','worker_pid':None}],
    [{'ended_at':1,'status':'failed'}],[{'ended_at':None,'status':'running'}]])
def test_preflight_admits_only_unstarted_plan_with_exact_closed_evidence(initial,monkeypatch,runs):
    p4,w,grant,plan,run=initial
    w.update(state='cancelled',cancel_requested=True,current_stage='release_planning',release_id=None,revision=7)
    w['scope']['recovery_policy']=copy.deepcopy(DEFAULT)
    w['release_recovery_history']=[]
    card={'node':'vault-finalize-plan','task_id':'first-plan','idempotency_key':'key'}
    w['kanban']={'board_id':'board','task_map':[card]}
    assert needs_preflight(p4.vault,w)
    basis=grant_request(p4,grant)['base_release']
    # The exact deterministic path is tested by production; keep the fixture's
    # real plan bytes and closure while controlling its isolated release name.
    monkeypatch.setattr(p4,'_plan_path',lambda _:p4.vault/basis['ref'])
    service=NS(vault=p4.vault,status=lambda _:copy.deepcopy(w),_lock=lambda _:p4.vault/'lock',
        _check_request=lambda q:None,assert_execution_ready=lambda v:None,
        _mutation=lambda q:copy.deepcopy(w),pending_pause=lambda v:False,
        _write=lambda value:w.update(value))
    adapter=NS(workflow=service,kanban=NS(task_snapshot_fresh=lambda *a:{'runs':runs}))
    monkeypatch.setattr(release_recovery,'FileVaultFinalizeService',lambda _:p4)
    monkeypatch.setattr(release_recovery,'verify_cancel_drain',lambda *a,**k:None)
    monkeypatch.setattr(release_recovery,'_exclusive_lock',lambda *a,**k:nullcontext())
    monkeypatch.setattr(ingest_kanban,'desired_graph',lambda *a:[NS(name='build-finalize'),NS(name='vault-finalize-plan',input_fingerprint='fp')])
    monkeypatch.setattr(ingest_kanban,'domain_completed',lambda *a:True)
    q={'workflow_id':'fixture','actor':'agent','expected_revision':7,'recovery_id':'preflight',
       'reason':'initial qualification','recovery_scope':'initial-release-preflight/v1',
       'task_id':'first-plan','input_fingerprint':'fp','blocked_plan':basis,
       'governance_receipt':grant_request(p4,grant)['source_governance_receipt'],'initial_page_governance':grant}
    # Existing isolated source receipt is real but this API additionally checks
    # its count; retain all its identities and update the grant pin accordingly.
    import json
    receipt_path=p4.vault/q['governance_receipt']['ref']
    receipt=json.loads(receipt_path.read_text());receipt['resolved']=len(receipt['rows'])
    q['governance_receipt']=save(p4.vault,q['governance_receipt']['ref'],receipt)
    g=json.loads((p4.vault/grant['ref']).read_text());g['request']['source_governance_receipt']=q['governance_receipt']
    g['request']['input_digest']='sha256:'+fingerprint({k:v for k,v in g['request'].items() if k!='input_digest'})
    q['initial_page_governance']=save(p4.vault,grant['ref'],g)
    q['input_digest']='sha256:'+fingerprint(q)
    if runs and runs[0]['status']!='scheduled':
        with pytest.raises(ContractError,match='no native attempt'):release_recovery.recover(adapter,q)
    else:
        result=release_recovery.recover(adapter,q)
        assert result['domain_calls_granted']==0
        assert admitted_pointer(p4.vault,w)==q['initial_page_governance']
        assert not needs_preflight(p4.vault,w)
        report=json.loads((p4.vault/result['report_ref']).read_text())
        assert report['failure'] is None and report['native_runs']==runs
        assert release_recovery.recover(adapter,q)['replayed']
