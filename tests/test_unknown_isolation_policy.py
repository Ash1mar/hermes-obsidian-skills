"""Old immutable startup scope requires one audited, drained policy adoption."""
from copy import deepcopy
from types import SimpleNamespace as NS
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
import pytest
from hermes_source_units import ContractError, mutation_digest
from hermes_source_units.recovery_policy import DEFAULT, effective
from hermes_source_units.validation import fingerprint
from unknown_isolation_policy import adopt


def setup(tmp_path,monkeypatch):
    policy={**deepcopy(DEFAULT),'unknown_completion':'hold'}
    w={'workflow_id':'ingest-old','actor':'operator','revision':3,'state':'cancelled',
       'cancel_requested':True,'scope':{'recovery_policy':policy}}
    q={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':3,
       'operation_id':'adopt-1','reason':'Workflow-wide authorized isolation',
       'unknown_completion':'isolate_after_drain','previous_policy_sha256':fingerprint(policy)}
    q['input_digest']=mutation_digest(q)
    service=NS(vault=tmp_path,status=lambda _:deepcopy(w),_lock=lambda _:tmp_path/'workflow.lock',
               pending_pause=lambda _:False,_write=lambda value:w.update(value))
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService
    service._check_request=FileIngestWorkflowService._check_request
    def mutate(request):
        if request['expected_revision']!=w['revision']:raise ContractError('REVISION_CONFLICT','$','changed')
        return deepcopy(w)
    service._mutation=mutate
    monkeypatch.setattr('orchestration.configuration',lambda:{})
    monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',lambda *a,**kw:None)
    return NS(workflow=service),w,q


def test_adoption_preserves_scope_and_replays_after_revision_change(tmp_path,monkeypatch):
    a,w,q=setup(tmp_path,monkeypatch);scope=deepcopy(w['scope'])
    assert not adopt(a,q,dry_run=True)['applied'] and w['revision']==3
    assert adopt(a,q)['applied'] and w['revision']==4
    assert adopt(a,q)['replayed'] and w['revision']==4
    assert w['scope']==scope and effective(w,{})['unknown_completion']=='isolate_after_drain'
    changed={**q,'reason':'changed'};changed['input_digest']=mutation_digest(changed)
    with pytest.raises(ContractError,match='IDEMPOTENCY_CONFLICT'):adopt(a,changed)
    w['unknown_isolation_adoptions'][0]['previous_policy_sha256']='0'*64
    with pytest.raises(ContractError,match='STALE_INPUT'):effective(w,{})


@pytest.mark.parametrize('fault',['pause','running','hash','actor','worker','drain'])
def test_adoption_does_not_bypass_safety(tmp_path,monkeypatch,fault):
    a,w,q=setup(tmp_path,monkeypatch)
    if fault=='pause':a.workflow.pending_pause=lambda _:True
    if fault=='running':w.update(state='analyzing',cancel_requested=False)
    if fault=='hash':q['previous_policy_sha256']='0'*64
    if fault=='actor':q['actor']='foreign'
    if fault=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    if fault=='drain':
        def reject(*a,**kw):raise ContractError('WORKER_ACTIVE','$','live')
        monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',reject)
    q['input_digest']=mutation_digest(q)
    with pytest.raises(ContractError):adopt(a,q)
    assert w['revision']==3 and not w.get('unknown_isolation_adoptions')


def test_new_default_does_not_silently_change_legacy_or_pinned_hold():
    assert DEFAULT['unknown_completion']=='isolate_after_drain'
    assert effective({'scope':{}},{})['unknown_completion']=='hold'
    assert effective({'scope':{'recovery_policy':{**DEFAULT,'unknown_completion':'hold'}}},{})['unknown_completion']=='hold'
