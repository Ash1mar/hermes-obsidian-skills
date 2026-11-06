"""Isolated node boundary/control tests. No real native/model workflow runs."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from test_p3_knowledge_build import vault,FileIngestWorkflowService,workflow_request,ROOT
from hermes_source_units.source_units import _write_atomic,_json_bytes
from hermes_source_units.validation import ContractError,fingerprint
from hermes_source_units.workflow_test import NODES,STAGES,enable,capture,manifest,inventory,root,check_admission,CONTRACT
from workflow_test_entry import describe,reenter,_apply,digest,handoff
from ingest_kanban import Node,pass_node,node_identity,IngestKanbanAdapter


@pytest.fixture
def controlled(vault,monkeypatch):
    p=vault/'_system/vault.json';v=json.loads(p.read_text());v['vault']['security_domain']='isolated-test';p.write_text(json.dumps(v))
    _write_atomic(vault/'_system/metadata/retrieval-policy.json',_json_bytes({'contract':'vault-retrieval-policy/v1','sync_enabled':False,'query_enabled':False}))
    raw=vault/'10_Raw/original.md';raw.write_text('Keep original conditions, exceptions, QA and evidence.')
    service=FileIngestWorkflowService(vault)
    w=service.start(workflow_request(workflow_id='ingest-node-fixture',actor='agent',profile='compact-5',expected_revision=0,
        scope={'source_paths':['10_Raw/original.md'],'knowledge_selector':'all','execution_mode':'auto_full','pause_after':[]}))
    fake=NS(task_states=lambda _: {},task_snapshot=lambda *_: {'task':{},'runs':[]})
    adapter=NS(workflow=service,kanban=fake)
    monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[])
    enable(vault,w['workflow_id'],'agent')
    return vault,service,w,adapter


@pytest.mark.parametrize('kind',[n for n in NODES if n!='bootstrap'])
def test_each_node_has_a_distinct_hash_bound_data_boundary(controlled,kind):
    vault,service,w,adapter=controlled
    w.update(current_stage=STAGES[kind][0],state=STAGES[kind][0])
    if kind in ('canary','pass'):w['dispatch_policy']['mode']='canary' if kind=='canary' else 'full'
    service._write(w)
    node=Node('fixture','pass-slice' if kind in ('canary','pass') else kind,(),'sha256:'+'a'*64)
    before=(vault/'10_Raw/original.md').read_bytes()
    assert capture(adapter,w,node)
    value,pin=manifest(vault,w['workflow_id'],kind)
    assert value['node']==kind and value['capture_grants_execution'] is False
    assert value['raw_sources']['10_Raw/original.md']==digest(before)
    assert not any('test-boundaries' in Path(ref).parts for ref in value['files'])
    assert capture(adapter,w,node) and manifest(vault,w['workflow_id'],kind)[1]==pin
    assert (vault/'10_Raw/original.md').read_bytes()==before
    if kind=='source-prepare':assert manifest(vault,w['workflow_id'],'bootstrap')[0]['node']=='bootstrap'


def test_default_formal_dispatch_does_not_capture_or_wait(controlled):
    vault,service,w,adapter=controlled
    (vault/'_system/metadata/workflow-test-policy.json').unlink()
    assert capture(adapter,w,Node('fixture','source-prepare',(),'sha256:'+'a'*64))
    assert not root(vault,w['workflow_id']).exists()


def test_native_prefix_must_drain_before_first_capture(controlled):
    vault,service,w,adapter=controlled
    w['kanban']['task_map']=[{'node':'source-prepare:x','task_id':'native','idempotency_key':'ingest:fixture'}]
    service._write(w)
    adapter.kanban.task_snapshot=lambda *_:{'task':{'worker_pid':123},'runs':[]}
    assert not capture(adapter,w,Node('fixture','source-prepare',(),'sha256:'+'a'*64))
    assert not root(vault,w['workflow_id']).exists()


def test_reentry_is_once_only_keeps_same_identity_and_never_runs_model(controlled):
    vault,service,w,adapter=controlled
    node=Node('fixture','source-prepare',(),'sha256:'+'a'*64)
    assert capture(adapter,w,node)
    boundary,pin=manifest(vault,w['workflow_id'],'source-prepare')
    # Simulate later managed control data, not semantic acceptance evidence.
    old=vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/pass-call-budget.json'
    _write_atomic(old,b'{"historical_attempts":7}')
    w.update(state='partial',current_stage='validating',revision=w['revision']+1);service._write(w)
    request=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        test_mode=CONTRACT,from_node='source-prepare',operation_id='node-cycle',overwrite_downstream=True,
        boundary_sha256=pin,pass_capacity=10)
    before=inventory(vault)
    assert reenter(adapter,request,dry_run=True)['applied'] is False
    assert inventory(vault)==before
    result=reenter(adapter,request);current=service.status(w['workflow_id'])
    assert result['applied'] and current['workflow_id']==w['workflow_id']
    assert current['start_digest']==w['start_digest'] and current['cancel_requested']
    assert current['revision']==w['revision']+1 and current['workflow_test']['pass_capacity']==10
    journal=vault/result['report_ref'];retired=journal.parent/'retired'/old.relative_to(vault)
    assert retired.read_bytes()==b'{"historical_attempts":7}' and not old.exists()
    check_admission(service,current)
    assert reenter(adapter,request)['replayed'] and service.status(w['workflow_id'])==current


@pytest.mark.parametrize('fault',['raw','user_note','boundary','query','running','pause'])
def test_changed_inputs_or_active_state_block_before_writes(controlled,fault):
    vault,service,w,adapter=controlled
    capture(adapter,w,Node('fixture','source-prepare',(),'sha256:'+'a'*64))
    boundary,pin=manifest(vault,w['workflow_id'],'source-prepare')
    w.update(state='partial',current_stage='validating',revision=w['revision']+1)
    if fault=='running':w['state']='reducing'
    if fault=='pause':w['scope']['pause_after']=['checkpoint_1']
    service._write(w)
    if fault=='raw':(vault/'10_Raw/original.md').write_text('Changed source')
    if fault=='user_note':(vault/'20_Notes/user.md').write_text('Unrelated user note')
    if fault=='boundary':(root(vault,w['workflow_id'])/'blobs'/next(iter(boundary['files'].values()))).write_text('changed')
    if fault=='query':
        p=vault/'_system/metadata/retrieval-policy.json';v=json.loads(p.read_text());v['query_enabled']=True;p.write_text(json.dumps(v))
    request=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        test_mode=CONTRACT,from_node='source-prepare',operation_id='bad-cycle',overwrite_downstream=True,boundary_sha256=pin,pass_capacity=10)
    before=inventory(vault)
    with pytest.raises(ContractError):reenter(adapter,request)
    assert inventory(vault)==before


def test_historical_missing_boundary_is_explicit_and_does_not_fabricate(controlled):
    vault,service,w,adapter=controlled
    with pytest.raises(ContractError,match='no authenticated boundary'):
        describe(adapter,w['workflow_id'],'acceptance')
    assert not root(vault,w['workflow_id']).exists()


def test_interrupted_binary_restore_is_idempotent(tmp_path):
    from hermes_source_units.workflow_test import root
    wid='ingest-fixture';binary=b'\x00\xffregistered asset';h=digest(binary)
    _write_atomic(root(tmp_path,wid)/'blobs'/h,binary)
    _write_atomic(tmp_path/'_system/asset.bin',b'old asset')
    j=tmp_path/f'_system/ledgers/ingest-workflows/{wid}/workflow-tests/cycle/transaction.json'
    plan={'request':{'workflow_id':wid},'phase':'prepared','retired_files':{'_system/asset.bin':digest(b'old asset')},
        'writes':{'_system/asset.bin':{'blob':h,'sha256':h}},'preserved_files':{}}
    _write_atomic(j,_json_bytes(plan))
    _apply(tmp_path,j,copy.deepcopy(plan));_apply(tmp_path,j,copy.deepcopy(plan))
    assert (tmp_path/'_system/asset.bin').read_bytes()==binary
    assert (j.parent/'retired/_system/asset.bin').read_bytes()==b'old asset'


def test_stopped_replay_never_releases_a_later_user_cancel(controlled,monkeypatch):
    vault,service,w,adapter=controlled
    w.update(state='cancelled',cancel_requested=True,current_stage='source_preparing',resume_stage='source_preparing',revision=20)
    service._write(w)
    monkeypatch.setattr('orchestration.dispatch',lambda *_:pytest.fail('must not dispatch'))
    result=handoff(adapter,{'workflow_id':w['workflow_id'],'expected_revision':1},{'ok':True})
    assert result['workflow']['cancel_requested'] and 'handoff' not in result


def test_default_cli_exposes_every_node_and_same_workflow_mineru_aliases(monkeypatch):
    spec=importlib.util.spec_from_file_location('node_test_cli',ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/start_workflow_test.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.ALIASES['post-mineru']=='exact-plan' and module.ALIASES['mineru']=='source-prepare'
    monkeypatch.setattr(module,'ensure_native_runtime',lambda:None)
    monkeypatch.setattr('sys.argv',['start_workflow_test.py','--help'])
    with pytest.raises(SystemExit) as result:module.main()
    assert result.value.code==0


def test_completed_pass_prefix_identity_is_frozen_and_missing_work_changes():
    w={'workflow_id':'ingest-frozen','workflow_test':{'operation_id':'new-cycle',
        'prefix_fingerprints':{'pass-slice:accepted':'sha256:'+'a'*64}}}
    accepted={'slice_id':'accepted','input_fingerprint':'sha256:'+'b'*64,'template_hash':'sha256:'+'c'*64}
    assert pass_node(w,accepted).input_fingerprint=='sha256:'+'a'*64
    pending={**accepted,'slice_id':'pending'}
    assert pass_node(w,pending).input_fingerprint!=pass_node({'workflow_id':w['workflow_id']},pending).input_fingerprint


def test_capture_can_be_reenabled_only_for_same_test_identity(controlled,monkeypatch):
    vault,service,w,adapter=controlled
    before=(vault/'_system/metadata/workflow-test-policy.json').read_bytes()
    assert enable(vault,w['workflow_id'],w['actor'])['capture']
    assert (vault/'_system/metadata/workflow-test-policy.json').read_bytes()==before
    monkeypatch.setenv('HERMES_KANBAN_TASK','isolated-worker')
    with pytest.raises(ContractError,match='trusted'):enable(vault,w['workflow_id'],w['actor'])
