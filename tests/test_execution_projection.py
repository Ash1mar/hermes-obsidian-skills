"""Isolated operation-local parsing; bytes/identities remain live checks."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_compact_execution import vault
from hermes_source_units import FileIngestWorkflowService, ContractError
from hermes_source_units.validation import fingerprint
import hermes_source_units.execution_plan as execution
from ingest_kanban import desired_graph, pass_node


def setup_plan(vault):
    service=FileIngestWorkflowService(vault)
    root=vault/'_system/ledgers/ingest-workflows/ingest-cache/execution-plans/test'
    root.mkdir(parents=True)
    template='<!-- isolated template -->\n'
    (root/'worker.md').write_text(template)
    plan={'workflow_id':'ingest-cache','batch_id':'cache-batch','new_slice_ids':['s1','s2'],
        'previous':None,'template_ref':(root/'worker.md').relative_to(vault).as_posix(),
        'template_hash':hashlib.sha256(template.encode()).hexdigest()}
    plan['plan_hash']=fingerprint(plan)
    path=root/'manifest.json';path.write_text(json.dumps(plan))
    workflow={'workflow_id':'ingest-cache','batch_id':'cache-batch',
        'execution_plan':{'path':path.relative_to(vault).as_posix(),'sha256':plan['plan_hash']}}
    return service,workflow,path,plan


def test_execution_projection_reuses_parse_but_checks_bytes_and_expires(vault,monkeypatch):
    service,workflow,path,plan=setup_plan(vault)
    original=execution.json.loads;reads=[]
    def loads(value,*args,**kwargs):
        reads.append(value);return original(value,*args,**kwargs)
    monkeypatch.setattr(execution.json,'loads',loads)
    with service.execution_projection():
        first=service.execution_plan(workflow)
        first['new_slice_ids'].append('foreign')
        assert service.execution_plan(workflow)['new_slice_ids']==['s1','s2']
        assert service.execution_template(workflow,'s1')==service.execution_template(workflow,'s2')
        assert len(reads)==1
        changed=copy.deepcopy(workflow);changed['execution_plan']['sha256']='0'*64
        with pytest.raises(ContractError,match='STALE_INPUT'):service.execution_plan(changed)
        assert len(reads)==1
        broken=copy.deepcopy(plan);broken['new_slice_ids'].append('tampered')
        path.write_text(json.dumps(broken))
        with pytest.raises(ContractError,match='STALE_INPUT'):service.execution_plan(workflow)
        assert len(reads)==2
        path.write_text(json.dumps(plan))
        template=vault/plan['template_ref'];template.write_text('modified')
        with pytest.raises(ContractError,match='TEMPLATE_CHANGED'):service.execution_template(workflow,'s1')
    assert service._execution_projection_cache is None
    service.execution_plan(workflow)
    assert len(reads)==3


def test_analyzing_graph_does_not_scan_unopened_reduce_tasks(vault):
    slices=[{'slice_id':'s1','input_fingerprint':'sha256:'+'1'*64,'template_hash':'2'*64,'task_ids':['t1']},
        {'slice_id':'s2','input_fingerprint':'sha256:'+'3'*64,'template_hash':'4'*64,'task_ids':['t2']}]
    def unopened(_):raise AssertionError('unopened Reduce must not read tasks or Passes')
    knowledge=SimpleNamespace(_batch=lambda _: {'task_ids':['t1','t2']},_slices=lambda _:slices,_task=unopened)
    service=SimpleNamespace(vault=vault,knowledge=knowledge,pending_pause=lambda _:None)
    workflow={'workflow_id':'ingest-graph','batch_id':'batch','current_stage':'analyzing',
        'scope':{'pause_after':['pass'],'execution_mode':'auto_full'},'dispatch_policy':{'mode':'full'}}
    assert desired_graph(service,workflow)==[pass_node(workflow,s) for s in slices]
    workflow['current_stage']='reducing'
    with pytest.raises(AssertionError,match='unopened Reduce'):
        desired_graph(service,workflow)


def test_template_lookup_avoids_copy_but_public_plan_remains_isolated(vault,monkeypatch):
    service,workflow,path,plan=setup_plan(vault)
    original=execution.copy.deepcopy;copies=[]
    monkeypatch.setattr(execution.copy,'deepcopy',lambda value:copies.append(value) or original(value))
    with service.execution_projection():
        assert service.execution_template(workflow,'s1')=='<!-- isolated template -->\n'
        assert service.execution_template(workflow,'absent') is None
        assert copies==[]
        public=service.execution_plan(workflow)
        assert len(copies)==1
        public['new_slice_ids'].clear()
        assert service.execution_template(workflow,'s1') is not None
        changed=copy.deepcopy(workflow);changed['batch_id']='different'
        with pytest.raises(ContractError,match='STALE_INPUT'):service.execution_template(changed,'s1')
