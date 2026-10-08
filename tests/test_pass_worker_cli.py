"""Exercise the exact isolated CLI advertised by the Pass card."""
import json
import os
import subprocess
import sys

import pytest

from test_p3_knowledge_build import (vault, ROOT, plan_sliced_batch, FakeKanban,
    IngestKanbanAdapter, start_and_pin, workflow_request)


def test_pass_cli_uses_bound_lease_and_rejects_other_task(vault):
    batch = plan_sliced_batch(vault, 27, 'bound-pass-batch')
    adapter = IngestKanbanAdapter(vault, FakeKanban(True), enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id='ingest-bound-pass',
        actor='agent', expected_revision=0, profile='compact-3',
        scope={'source_paths':[], 'knowledge_selector':'all-current', 'execution_mode':'canary_only'},
        batch_id='bound-pass-batch'))
    adapter._sync_current(value['workflow_id'])
    value = adapter.workflow.status(value['workflow_id'])
    card = next(c for c in value['kanban']['task_map'] if c['node'].startswith('pass-slice:'))
    binding = {'workflow_id':value['workflow_id'],'node':card['node'],'task_id':card['task_id'],
               'actor':'agent','worker_id':'bound-test-worker'}
    workspace = vault/'kanban-workspace'
    workspace.mkdir()
    source_binding, binding_path = vault/'immutable-binding.json', workspace/'worker-request.json'
    source_binding.write_text(json.dumps(binding))
    env = {**os.environ,'HERMES_DELEGATED_CHILD_CONTEXT':'1',
           'HERMES_KANBAN_WORKSPACE':str(workspace)}
    env.pop('HERMES_KANBAN_TASK', None)
    dispatcher = [sys.executable,'-I','-S','-X','utf8',str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/dispatch_ingest_workflow.py'),
                  '--vault',str(vault)]
    begin = dispatcher+['worker-begin','--request',str(source_binding),'--request-output',str(vault/'outside.json')]
    rejected = subprocess.run(begin,capture_output=True,text=True,env=env)
    assert rejected.returncode==2 and 'worker workspace' in rejected.stderr
    assert not (vault/'outside.json').exists()
    assert batch._slice('bound-pass-batch', card['node'].removeprefix('pass-slice:'))['state']=='ready'
    begin[-1] = str(binding_path)
    started = subprocess.run(begin,capture_output=True,text=True,env=env)
    assert started.returncode==0,started.stderr
    begun = json.loads(started.stdout)
    binding = json.loads(binding_path.read_text())
    assert binding == begun['worker_request']
    assert binding_path.read_bytes().endswith(b'\n')
    assert binding['expected_revision'] == begun['slice']['revision']
    assert binding['template_hash'] == begun['template_hash']
    assert 'lease_revision' not in binding
    task_id = begun['task_ids'][0]
    task = batch._task(task_id)
    assert set(p['task_id'] for p in begun['task_snapshots']) == set(begun['task_ids'])
    assert all(batch._task(t)['status']=='pending' for t in batch._batch('bound-pass-batch')['task_ids']
               if t not in begun['task_ids'])
    package = next(p for p in begun['reading_packages'] if p['task_id']==task_id)
    assert (vault/package['path']).is_file()
    # Checking one worker must remain bounded to its own tasks, even at scale.
    original_task = adapter.workflow.knowledge._task
    def bounded_task(t):
        assert t in begun['task_ids'], 'worker check scanned another slice'
        return original_task(t)
    adapter.workflow.knowledge._task = bounded_task
    assert adapter.worker_check(binding)['ok']
    common = {'task_id':task_id,'actor':'agent','reading_package_id':package['reading_package_id'],
        'registry_revision':1,'expected_revision':batch._task(task_id)['revision'],
        'pass_kind':'candidate','sequence':0,
        'inspections':[{'source_ref':ref,'finding':'Fixture evidence inspected','qa':'usable','qa_note':''}
                       for ref in task['target_refs']],
        'candidates':[], 'empty_reason':'Fixture has no reusable candidate'}
    req = {'batch_id':'bound-pass-batch','passes':[common]}
    request_path = vault/'pass-request.json'
    before_check = {str(path):path.read_bytes() for path in (vault/'_system/ledgers').rglob('*.json')}
    check = subprocess.run(dispatcher+['worker-check','--request',str(binding_path),
        '--request-output',str(binding_path)],
        capture_output=True,text=True,env=env)
    assert check.returncode==0,check.stderr
    assert json.loads(binding_path.read_text()) == binding
    assert json.loads(check.stdout)['worker_request'] == binding
    assert before_check == {str(path):path.read_bytes() for path in (vault/'_system/ledgers').rglob('*.json')}
    command = [sys.executable,'-I','-S','-X','utf8',str(ROOT/'hermes-obsidian-controlled-ingest/scripts/manage_knowledge_build.py'),
        '--vault',str(vault),'--worker-binding',str(binding_path),'batch-pass','--request',str(request_path)]
    request_path.write_text(json.dumps(req))
    result = subprocess.run(command,capture_output=True,text=True,env=env)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['results'][0]['task_id']==task_id
    other = next(t for t in batch._batch('bound-pass-batch')['task_ids'] if t not in begun['task_ids'])
    req['passes'][0]['task_id'] = other
    request_path.write_text(json.dumps(req))
    before = batch._task(other)
    result = subprocess.run(command,capture_output=True,text=True,env=env)
    assert result.returncode==2 and 'ACCESS_DENIED' in result.stderr
    assert batch._task(other)==before
    # A current slice hash is mandatory, including after terminal env stripping.
    binding['template_hash']='sha256:'+'0'*64
    binding_path.write_text(json.dumps(binding))
    before_invalid_check = binding_path.read_bytes()
    invalid_check = subprocess.run(dispatcher+['worker-check','--request',str(binding_path),
        '--request-output',str(binding_path)],capture_output=True,text=True,env=env)
    assert invalid_check.returncode==2 and 'STALE_INPUT' in invalid_check.stderr
    assert binding_path.read_bytes() == before_invalid_check
    assert not list(workspace.glob('.worker-request-*'))
    result=subprocess.run(command,capture_output=True,text=True,env=env)
    assert result.returncode==2 and 'STALE_INPUT' in result.stderr
    binding_path.write_text(json.dumps(begun['worker_request']))
    refreshed = subprocess.run(dispatcher+['worker-heartbeat','--request',str(binding_path),
        '--request-output',str(binding_path)],capture_output=True,text=True,env=env)
    assert refreshed.returncode==0,refreshed.stderr
    heartbeat = json.loads(refreshed.stdout)
    assert heartbeat['worker_request']['expected_revision']==heartbeat['slice']['revision']
    assert heartbeat['worker_request']['expected_revision']>begun['worker_request']['expected_revision']
    assert json.loads(binding_path.read_text())==heartbeat['worker_request']
    assert adapter.worker_check(heartbeat['worker_request'])['ok']
    # Omitted output must refresh the same safe file, never leave a stale lease.
    refreshed = subprocess.run(dispatcher+['worker-heartbeat','--request',str(binding_path)],
        capture_output=True,text=True,env=env)
    assert refreshed.returncode == 0, refreshed.stderr
    automatic = json.loads(refreshed.stdout)
    assert json.loads(binding_path.read_text()) == automatic['worker_request']
    assert automatic['worker_request']['expected_revision'] > heartbeat['worker_request']['expected_revision']
    # A canonical/outside request is not an automatic rewrite target; reject
    # before renewing the lease and preserve its bytes.
    outside = vault/'outside-current-binding.json'
    outside.write_text(json.dumps(automatic['worker_request']))
    rejected = subprocess.run(dispatcher+['worker-heartbeat','--request',str(outside)],
        capture_output=True,text=True,env=env)
    assert rejected.returncode == 2 and 'worker workspace' in rejected.stderr
    assert adapter.worker_check(automatic['worker_request'])['ok']
    assert json.loads(outside.read_text()) == automatic['worker_request']
