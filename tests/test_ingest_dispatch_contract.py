"""Dispatcher artifacts, rejected requests and repeated native scheduling."""
import json
import os
import subprocess
import sys

import pytest

from test_ingest_worker_recovery import setup_worker
from test_p3_knowledge_build import (ROOT, vault, publish_sources, FakeKanban,
    IngestKanbanAdapter, start_and_pin, workflow_request, plan_sliced_batch)
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


def test_operator_repair_preview_requires_failure_report_evidence(tmp_path):
    target, adapter, value, _ = setup_worker(tmp_path)
    current = adapter.workflow.status(value['workflow_id'])
    adapter.workflow.cancel(workflow_request(workflow_id=value['workflow_id'], actor='agent',
        expected_revision=current['revision']))
    report_ref = f"_system/ledgers/ingest-workflows/{value['workflow_id']}/reports/failed-lock.json"
    report = target / report_ref
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"error_code":"LOCK_BUSY","source_failed":false}')
    command = [sys.executable, '-I', '-S', str(ROOT /
        'hermes-obsidian-governed-ingest-orchestrator/scripts/repair_preparation.py'),
        '--vault', str(target), '--workflow-id', value['workflow_id'], '--repair-id', 'lock-repair']
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode == 2 and 'ARTIFACT_REQUIRED' in missing.stderr
    nonexistent = subprocess.run(command + ['--evidence-ref', report_ref + '.missing'],
        capture_output=True, text=True)
    assert nonexistent.returncode == 2 and 'ARTIFACT_REQUIRED' in nonexistent.stderr
    preview = subprocess.run(command + ['--evidence-ref', report_ref], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    request = json.loads(preview.stdout)['request']
    assert request['reset_sources'] == []
    assert request['evidence_refs'] == [report_ref]
    assert adapter.workflow.status(value['workflow_id']).get('repair_history', []) == []


@pytest.mark.parametrize('change,code', [
    ({'template_hash':None},'INVALID_SCHEMA'),
    ({'worker_template_hash':'wrong'},'INVALID_SCHEMA'),
    ({'template_hash':'wrong'},'STALE_INPUT'),
    ({'task_id':'wrong'},'STALE_INPUT'),
])
def test_request_rejection_does_not_mutate_domain(tmp_path, change, code):
    target, adapter, value, req = setup_worker(tmp_path)
    req['template_hash'] = adapter.worker_begin(req)['template_hash']
    before = adapter.workflow._path(value['workflow_id']).read_bytes()
    with pytest.raises(ContractError, match=code):
        adapter.worker_check({**req, **change})
    assert adapter.workflow._path(value['workflow_id']).read_bytes() == before


def test_failure_archives_binding_and_dependents_and_denies_domain_write(tmp_path):
    target, adapter, value, req = setup_worker(tmp_path)
    req['template_hash'] = adapter.worker_begin(req)['template_hash']
    adapter.execution_failure(req, 'INVALID_SCHEMA', 'contract rejected')
    revision = adapter.workflow.status(value['workflow_id'])['revision']
    for _ in range(3):
        result = adapter._sync_current(value['workflow_id'])
        assert req['node'] in result['execution_blocked']
        assert adapter.kanban.waited[req['task_id']] == 'archived'
        assert adapter.workflow.status(value['workflow_id'])['revision'] == revision
    with pytest.raises(ContractError, match='EXECUTION_BLOCKED'):
        adapter.worker_check(req)
    with pytest.raises(ContractError, match='EXECUTION_BLOCKED'):
        with worker_binding(req), workflow_write_guard(target, actor='agent'):
            pytest.fail('failed binding entered domain write')
    assert not adapter.workflow.status(value['workflow_id'])['source_outcomes']
    # A supported explicit repair changes binding identity even if the template
    # bytes are unchanged. The historical failure is retained for audit.
    from orchestration import worker_pack
    current = adapter.workflow.status(value['workflow_id'])
    stopped = adapter.workflow.cancel(workflow_request(workflow_id=value['workflow_id'], actor='agent',expected_revision=current['revision']))
    repaired = adapter.workflow.repair_preparation(workflow_request(workflow_id=value['workflow_id'],actor='agent',
        expected_revision=stopped['revision'],repair_id='contract-repair',reason='Verified request recovery',
        evidence_refs=[stopped['template_pins'][0]['path']],templates=worker_pack(),reset_sources=[]))
    adapter.resume(workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=repaired['revision']))
    replacement = next(c for c in adapter.workflow.status(value['workflow_id'])['kanban']['task_map'] if c['node']==req['node'])
    assert replacement['task_id'] != req['task_id']
    assert adapter.worker_begin({**req,'task_id':replacement['task_id']})


def test_dispatcher_artifact_runs_exact_helper_in_isolated_terminal(vault):
    refs = publish_sources(vault, ['# Evidence\nSupported material.\n'])
    adapter = IngestKanbanAdapter(vault, FakeKanban(True), enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id='ingest-canonical', actor='agent',
        expected_revision=0, profile='compact-3', scope={'source_paths':['10_Raw/source-1.md'],
        'knowledge_selector':'fixed batch_id canonical-batch', 'execution_mode':'canary_only'}))
    adapter._sync_current(value['workflow_id'])
    card = next(c for c in adapter.workflow.status(value['workflow_id'])['kanban']['task_map']
                if c['node'].startswith('source-prepare:'))
    req = {'workflow_id':value['workflow_id'], 'node':card['node'], 'task_id':card['task_id']}
    begun = adapter.worker_begin(req)
    ref = refs[0]['unit_ref']
    adapter.worker_complete({**req, 'template_hash':begun['template_hash'],
        'resource_id':ref['resource_id'], 'unit_set_id':ref['unit_set_id']})
    from orchestration import worker_pack
    current = adapter.workflow.status(value['workflow_id'])
    stopped = adapter.workflow.cancel(workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=current['revision']))
    repaired = adapter.workflow.repair_preparation(workflow_request(workflow_id=value['workflow_id'],actor='agent',
        expected_revision=stopped['revision'],repair_id='ready-repair',reason='Preserve complete source preparation',
        evidence_refs=[stopped['template_pins'][0]['path']],templates=worker_pack(),reset_sources=[]))
    adapter.resume(workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=repaired['revision']))
    assert adapter.workflow.status(value['workflow_id'])['current_stage']=='planning'
    adapter._sync_current(value['workflow_id'])
    current = adapter.workflow.status(value['workflow_id'])
    card = next(c for c in current['kanban']['task_map'] if c['node']=='exact-plan')
    binding = vault / f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(card['idempotency_key'])}.json"
    record = json.loads(binding.read_text())
    assert record['task_id'] == card['task_id'] and record['template_hash']
    env = {**os.environ, 'HERMES_DELEGATED_CHILD_CONTEXT':'1'}
    env.pop('HERMES_KANBAN_TASK', None)
    result = subprocess.run([sys.executable, '-I', '-S', str(ROOT /
        'hermes-obsidian-governed-ingest-orchestrator/scripts/run_preparation_worker.py'),
        '--vault', str(vault), '--binding', str(binding)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert adapter.workflow.status(value['workflow_id'])['batch_id'] == 'canonical-batch'
    assert json.loads(binding.with_suffix('.execution.json').read_text())['state']=='completed'


@pytest.mark.parametrize('change', ['hash', 'malformed', 'deleted'])
def test_changed_dispatch_artifact_is_preserved_and_blocked(tmp_path, change):
    target, adapter, value, req = setup_worker(tmp_path)
    card = next(c for c in value['kanban']['task_map'] if c['task_id']==req['task_id'])
    binding = target/f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(card['idempotency_key'])}.json"
    record = json.loads(binding.read_text()); record['template_hash']='wrong'
    binding.write_text(json.dumps(record))
    if change == 'malformed':
        binding.write_text('{broken')
    elif change == 'deleted':
        binding.unlink()
    before=binding.read_bytes() if binding.exists() else None
    for _ in range(3):
        adapter._sync_current(value['workflow_id'])
        assert (binding.read_bytes() if binding.exists() else None)==before
        assert adapter.kanban.waited[req['task_id']]=='archived'
    assert adapter.workflow.status(value['workflow_id'])['source_outcomes']==[]


def test_runtime_exposes_execution_reason_without_ledger_write(tmp_path):
    target, adapter, value, req = setup_worker(tmp_path)
    adapter.execution_failure(req,'INVALID_SCHEMA','template_hash was missing')
    adapter.kanban.task_snapshot=lambda slug, task_id: {'task':{'id':task_id,'status':'archived'},'runs':[]}
    before=adapter.workflow._path(value['workflow_id']).read_bytes()
    observed=adapter.runtime_status(value['workflow_id'])
    card=next(c for c in observed['cards'] if c['task_id']==req['task_id'])
    assert card['state']=='execution_blocked' and card['error_code']=='INVALID_SCHEMA'
    assert 'template_hash' in card['reason']
    assert adapter.workflow._path(value['workflow_id']).read_bytes()==before


def test_native_failure_preserves_durable_slice_retry_but_blocks_unreported_failure(vault):
    from datetime import datetime, timezone, timedelta
    batch=plan_sliced_batch(vault,27,'native-retry-batch')
    fake=FakeKanban(True)
    adapter=IngestKanbanAdapter(vault,fake,enable_workers=True)
    value=start_and_pin(vault,workflow_request(workflow_id='ingest-native-retry',actor='agent',
        expected_revision=0,profile='compact-3',scope={'source_paths':[],
        'knowledge_selector':'all-current','execution_mode':'canary_only'},batch_id='native-retry-batch'))
    adapter._sync_current(value['workflow_id'])
    value=adapter.workflow.status(value['workflow_id'])
    card=next(c for c in value['kanban']['task_map'] if c['node'].startswith('pass-slice:'))
    req={'workflow_id':value['workflow_id'],'node':card['node'],'task_id':card['task_id'],'worker_id':'retry-worker'}
    begun=adapter.worker_begin(req)
    adapter.worker_fail({**req,'template_hash':begun['template_hash'],'expected_revision':begun['slice']['revision'],
        'code':'INVALID_MODEL_JSON','message':'invalid structured response','failed_output':'{broken'})
    sid=card['node'].partition(':')[2]
    failed=batch._slice('native-retry-batch',sid)
    failure_at=datetime.fromisoformat(failed['last_error']['at'].replace('Z','+00:00')).timestamp()
    native_start=[failure_at-1]
    fake.task_snapshot=lambda slug, task_id: {'task':{'id':task_id,'status':'blocked'},
        'runs':[{'id':1,'started_at':native_start[0],'ended_at':failure_at+1,'outcome':'blocked'}] if task_id==card['task_id'] else []}
    adapter._sync_current(value['workflow_id'])
    assert fake.waited[card['task_id']]=='scheduled'
    batch.refresh_ready_slices('native-retry-batch','agent',now=datetime.now(timezone.utc)+timedelta(hours=1))
    adapter._sync_current(value['workflow_id'])
    assert card['task_id'] in fake.unblocked
    native_start[0]=failure_at+2  # A later run ended without a new Vault failure.
    result=adapter._sync_current(value['workflow_id'])
    assert card['node'] in result['execution_blocked']
    assert fake.waited[card['task_id']]=='archived'
