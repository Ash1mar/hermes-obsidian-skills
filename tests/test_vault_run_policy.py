"""Isolated configuration/startup mechanics; no native workflow or model calls."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_p3_knowledge_build import (vault, plan_sliced_batch, workflow_request,
    start_and_pin, FileIngestWorkflowService, ContractError, ROOT, FakeKanban, IngestKanbanAdapter)
from test_refill_handoff import refill
from hermes_source_units.runtime_policy import canary_count, vault_provider_config, POLICY_REF, check_provider_pin
from hermes_source_units.ingest_workflow import display_phase


def test_small_canary_is_pinned_and_legacy_still_requires_eight(vault):
    batch=plan_sliced_batch(vault,6,'small')
    batch.initialize_slices('small','agent',batch._batch('small')['revision'])
    scope={'source_paths':[],'knowledge_selector':'all','execution_mode':'auto_full','canary_policy':'sample'}
    w=start_and_pin(vault,workflow_request(workflow_id='ingest-small',actor='agent',expected_revision=0,
        profile='compact-5',scope=scope,batch_id='small'))
    service=FileIngestWorkflowService(vault)
    n=canary_count(service,w); assert 0<n<8
    preview=service.canary_preview(w['workflow_id'],8)
    assert len(preview['slices'])==n
    if n>1:
        with pytest.raises(ContractError,match='INVALID_SCHEMA'):
            service.arm_canary(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
                slice_ids=[preview['slices'][0]['slice_id']],selection_digest=preview['selection_digest']))
    arm=service.arm_canary(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
        slice_ids=[s['slice_id'] for s in preview['slices']],selection_digest=preview['selection_digest']))
    assert service.canary_preview(w['workflow_id'],8)['selection_digest']==preview['selection_digest']
    with pytest.raises(ContractError,match='INCOMPLETE_COVERAGE'):
        service.promote_canary(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=arm['revision']))
    legacy=copy.deepcopy(w);legacy['scope'].pop('canary_policy')
    assert canary_count(service,legacy)==8
    assert display_phase(w)['subphase']=='map_pass'


def test_off_is_explicit_and_does_not_fabricate_selection(vault):
    batch=plan_sliced_batch(vault,3,'off')
    batch.initialize_slices('off','agent',batch._batch('off')['revision'])
    scope={'source_paths':[],'knowledge_selector':'all','execution_mode':'auto_full','canary_policy':'off'}
    w=start_and_pin(vault,workflow_request(workflow_id='ingest-off',actor='agent',expected_revision=0,
        profile='compact-5',scope=scope,batch_id='off'))
    service=FileIngestWorkflowService(vault)
    result=service.skip_canary(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
    assert result['dispatch_policy']=={'mode':'full','slice_ids':[],'selection_digest':None}
    assert not any(s['result_refs'] for s in service.knowledge._slices('off'))


def test_legacy_eight_slice_arm_reconciles_without_new_policy(vault):
    plan_sliced_batch(vault,27,'legacy')
    w=start_and_pin(vault,workflow_request(workflow_id='ingest-legacy',actor='agent',expected_revision=0,
        profile='diagnostic-6',scope={'source_paths':[],'knowledge_selector':'all'},batch_id='legacy'))
    fake=FakeKanban(True);adapter=IngestKanbanAdapter(vault,fake,enable_workers=True)
    adapter.sync(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
    preview=adapter.workflow.canary_preview(w['workflow_id'],8)
    assert len(preview['slices'])==8
    current=adapter.workflow.status(w['workflow_id'])
    adapter.arm_canary(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        slice_ids=[s['slice_id'] for s in preview['slices']],selection_digest=preview['selection_digest']))
    assert len(fake.unblocked)==2
    assert len(adapter.workflow.status(w['workflow_id'])['dispatch_policy']['slice_ids'])==8


def test_provider_is_vault_local_and_pin_detects_change(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'; p=a/POLICY_REF;p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'contract':'vault-retrieval-policy/v1','sync_enabled':True,'query_enabled':False}))
    base={'enabled':False,'command':['no-execution']}
    assert vault_provider_config(base,a,'sync')['enabled']
    assert not vault_provider_config(base,a,'query')['enabled']
    assert not vault_provider_config(base,b,'sync')['enabled']
    assert not vault_provider_config({**base,'available':False},a,'sync')['enabled']
    scope={'provider_policy_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    check_provider_pin(a,scope);p.write_text('{}')
    with pytest.raises(ContractError,match='STALE_INPUT'): check_provider_pin(a,scope)


def test_new_pass_capacity_five_does_not_raise_other_workers(refill):
    import native_program_dispatch as d
    old=refill.adapter.workflow.status
    refill.adapter.workflow.status=lambda wid:{**old(wid),'scope':{'execution_policy':{'pass_worker_concurrency':5}}}
    refill.adapter.workflow.knowledge._batch=lambda _: {'slice_config':{'pass_worker_concurrency':5}}
    for i in range(3,7):
        card={'task_id':f't{i}','node':f'pass-slice:s{i}','idempotency_key':f'key-{i}'}
        task=SimpleNamespace(**vars(refill.tasks[0]));task.id=f't{i}';task.idempotency_key=f'key-{i}'
        task.body=json.dumps({'executor':'semantic-v1','workflow_id':'ingest-refill','worker_binding':task.id})
        refill.cards.append(card);refill.tasks.append(task)
    assert len(d.dispatch_programs(refill.adapter,'ingest-refill'))==5
    assert refill.adapter.program_worker_concurrency==2


def test_short_startup_reuses_bootstrap_and_observes_existing_workflow(tmp_path,monkeypatch):
    path=ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/start_vault_workflow.py'
    spec=importlib.util.spec_from_file_location('startup_fixture',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    calls=[]
    def isolated(vault,command,request):
        calls.append(command); return start_and_pin(vault,request)
    monkeypatch.setattr(module,'dispatch',isolated)
    source=tmp_path/'one.md';source.write_text('source fixture')
    request={'operation_id':'fixture','actor':'agent','vault_path':str(tmp_path/'new'),'vault_id':'fixture',
        'vault_name':'Fixture','security_domain':'test','sources':[str(source)]}
    record=tmp_path/'receipt.json';bootstrap=ROOT/'hermes-obsidian-vault-bootstrap'
    first=module.start(request,record,bootstrap);second=module.start(request,record,bootstrap)
    assert calls==['start']
    assert first['startup']['workflow_id']==second['startup']['workflow_id']
    assert second['workflow']['current_stage']=='created'
    assert second['startup']['execution_policy']['pass_worker_concurrency']==5
    source.write_text('changed')
    with pytest.raises(ValueError,match='changed'): module.start(request,record,bootstrap)
