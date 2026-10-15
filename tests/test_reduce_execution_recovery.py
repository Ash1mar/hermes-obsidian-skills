"""Isolated cancellation/recovery mechanisms, never real-Vault semantic acceptance."""
import copy
import hashlib
import json

import pytest

from test_p3_knowledge_build import (vault, prepare_layered_reduce_batch, IngestKanbanAdapter,
                                    start_and_pin, workflow_request)
from test_pass_revision import ArchivedKeyKanban
from hermes_source_units import ContractError, mutation_digest
from hermes_source_units.validation import fingerprint
from ingest_kanban import desired_graph
import fixed_reduce


class EndedKanban(ArchivedKeyKanban):
    def __init__(self):
        super().__init__()
        self.ended = set()

    def task_snapshot(self, slug, task_id):
        snap = super().task_snapshot(slug, task_id)
        if task_id in self.ended:
            snap['runs'] = [{'id':1, 'started_at':1, 'ended_at':2}]
        return snap

    def task_states(self, slug):
        return {tid:self.task_snapshot(slug, tid)['task']['status'] for tid in self.by_id}


def sync(adapter, wid):
    value = adapter.workflow.status(wid)
    adapter.sync(workflow_request(workflow_id=wid, actor=value['actor'], expected_revision=value['revision']))
    return adapter.workflow.status(wid)


def resource_fixture(adapter, card):
    path = adapter.kanban.by_id[card['task_id']]['body']['worker_binding']
    binding = json.loads(open(path, encoding='utf-8').read())
    ref, value = fixed_reduce.prepare(adapter, binding)
    handles = [c['candidate'] for c in value['view']['candidates']]
    result = fixed_reduce.submit(adapter, binding, ref, {
        'proposals':[{'candidate_refs':handles, 'summary':'Isolated fixture grouping',
            'identity_hints':[], 'path_hints':[]}], 'omitted_candidate_refs':[], 'reason':'Isolated mechanism fixture'})
    assert result['ok']
    assert adapter.worker_complete(binding)['ok']


def stopped_reducer(vault, monkeypatch, kind='resource-reduce'):
    prepare_layered_reduce_batch(vault, 'recovery-batch')
    adapter = IngestKanbanAdapter(vault, EndedKanban(), enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id='ingest-reduce-recovery', actor='agent',
        expected_revision=0, profile='compact-3', batch_id='recovery-batch', scope={
            'source_paths':[], 'knowledge_selector':'all-current', 'execution_mode':'auto_full',
            'provider':'skip', 'pause_after':['checkpoint_1']}))
    value['dispatch_policy'] = {'mode':'full', 'slice_ids':[], 'selection_digest':None}
    adapter.workflow._write(value)
    value = sync(adapter, value['workflow_id'])
    for card in value['kanban']['task_map']:
        if card['node'].startswith('pass-slice:'):
            begun = adapter.worker_begin({'workflow_id':value['workflow_id'], 'node':card['node'],
                                          'task_id':card['task_id'], 'worker_id':'fixture'})
            assert adapter.worker_complete(begun['worker_request'])['ok']
    value = sync(adapter, value['workflow_id'])
    resources = [c for c in value['kanban']['task_map'] if c['node'].startswith('resource-reduce:')]
    for card in resources[:None if kind == 'global-reduce' else 1]:
        resource_fixture(adapter, card)
        value = sync(adapter, value['workflow_id'])
    card = next(c for c in value['kanban']['task_map'] if
                c['node'] == ('global-reduce' if kind == 'global-reduce' else resources[-1]['node']))
    ref = adapter.execution_failure({'workflow_id':value['workflow_id'], 'node':card['node'],
                                    'task_id':card['task_id']}, 'NATIVE_WORKER_STOPPED', 'Isolated bounded cancellation')
    adapter.kanban.ended.add(card['task_id'])
    adapter.cancel(workflow_request(workflow_id=value['workflow_id'], actor='agent', expected_revision=value['revision']))
    value = adapter.workflow.status(value['workflow_id'])
    monkeypatch.setattr('native_stop_inventory.workflow_processes', lambda *_: [])
    request = workflow_request(workflow_id=value['workflow_id'], actor='agent', expected_revision=value['revision'],
        recovery_id='isolated-reduce-retry', recovery_scope='reduce-cancel/v1', reason='Authorized stopped native retry',
        entries=[{'node':card['node'], 'task_id':card['task_id'],
                  'failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest()}])
    return adapter, value, card, request, ref


@pytest.mark.parametrize('kind', ['resource-reduce', 'global-reduce'])
def test_cancelled_reduce_recovery_preserves_domain_and_pins_new_native_identity(vault, monkeypatch, kind):
    adapter, value, old, request, ref = stopped_reducer(vault, monkeypatch, kind)
    original = {p:p.read_bytes() for p in vault.rglob('*.json')}
    protected = {p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*') if p.is_file()}
    assert adapter.recover_reduce_execution(request, dry_run=True)['applied'] is False
    assert original == {p:p.read_bytes() for p in vault.rglob('*.json')}
    result = adapter.recover_reduce_execution(request)
    current = adapter.workflow.status(value['workflow_id'])
    assert current['state'] == 'cancelled' and current['scope']['pause_after'] == value['scope']['pause_after']
    assert not result['resumed'] and all(p.read_bytes() == b for p,b in protected.items())
    assert adapter.recover_reduce_execution(request)['replayed']
    saved = json.loads((vault/result['report_ref']).read_text())
    assert saved['entries'][0]['failure'] == json.loads((vault/ref).read_text())
    adapter.resume(workflow_request(workflow_id=value['workflow_id'], actor='agent', expected_revision=current['revision']))
    resumed = adapter.workflow.status(value['workflow_id'])
    fresh = next(c for c in resumed['kanban']['task_map'] if c['node'] == old['node'])
    assert fresh['task_id'] != old['task_id'] and fresh['idempotency_key'] != old['idempotency_key']
    assert resumed['current_stage'] == 'reducing'
    assert all(p.read_bytes() == b for p,b in protected.items() if 'passes' in p.parts or 'readings' in p.parts or 'resource-reductions' in p.parts)
    assert not any(c['node'] == 'build-finalize' for c in resumed['kanban']['task_map'])
    binding = json.loads((vault/f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(fresh['idempotency_key'])}.json").read_text())
    assert adapter.worker_check(binding)['ok']
    with pytest.raises(ContractError, match='STALE_INPUT'):
        adapter.worker_check({**binding, 'task_id':old['task_id']})
    assert not adapter._execution_blocked(value['workflow_id'], next(n for n in desired_graph(adapter.workflow, resumed) if n.name == fresh['node']))


@pytest.mark.parametrize('change,code', [
    ('failure_hash','STALE_INPUT'), ('actor','ACTOR_MISMATCH'), ('worker','ACCESS_DENIED'),
    ('live','WORKER_ACTIVE'), ('semantic','RECOVERY_NOT_ALLOWED'), ('budget','RECOVERY_NOT_ALLOWED'),
    ('ended','WORKER_ACTIVE'), ('template','STALE_INPUT'), ('pass_stage','INVALID_TRANSITION'),
])
def test_reduce_recovery_rejects_invalid_or_nonmechanical_authority(vault, monkeypatch, change, code):
    adapter, value, card, request, ref = stopped_reducer(vault, monkeypatch)
    if change == 'failure_hash': request['entries'][0]['failure_sha256'] = '0'*64
    if change == 'actor': request['actor'] = 'foreign'
    if change == 'worker': monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    if change == 'live': monkeypatch.setattr('native_stop_inventory.workflow_processes', lambda *_: [{'pid':1}])
    if change in ('semantic', 'budget'):
        failure = json.loads((vault/ref).read_text())
        failure['error_code'] = 'SEMANTIC_REVIEW_REQUIRED' if change == 'semantic' else 'READING_WINDOW_OVERSIZE'
        (vault/ref).write_text(json.dumps(failure))
        request['entries'][0]['failure_sha256'] = hashlib.sha256((vault/ref).read_bytes()).hexdigest()
    if change == 'ended': adapter.kanban.ended.clear()
    if change == 'template': adapter.kanban.by_id[card['task_id']]['body']['template_hash'] = 'changed'
    if change == 'pass_stage':
        value['current_stage'] = 'analyzing'; adapter.workflow._write(value)
    request['input_digest'] = mutation_digest(request)
    before = {p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError, match=code): adapter.recover_reduce_execution(request)
    assert before == {p:p.read_bytes() for p in vault.rglob('*.json')}


def test_reduce_recovery_is_one_retry_and_report_tamper_blocks_resume(vault, monkeypatch):
    adapter, value, card, request, ref = stopped_reducer(vault, monkeypatch)
    result = adapter.recover_reduce_execution(request)
    current = adapter.workflow.status(value['workflow_id'])
    again = copy.deepcopy(request)
    again.update(recovery_id='another-retry', expected_revision=current['revision'])
    again['input_digest'] = mutation_digest(again)
    with pytest.raises(ContractError, match='RECOVERY_EXHAUSTED'): adapter.recover_reduce_execution(again)
    (vault/result['report_ref']).write_text('{}')
    with pytest.raises(ContractError, match='STALE_INPUT'): adapter.workflow.assert_execution_ready(current)


@pytest.mark.parametrize('change', ['valid','original_attempted','replacement_attempted','body','binding','reason'])
def test_cancelled_unattempted_reduce_projection_recovery(vault,monkeypatch,change):
    adapter,value,old,request,ref=stopped_reducer(vault,monkeypatch)
    slug=value['kanban']['board_id'];node=next(n for n in desired_graph(adapter.workflow,value) if n.name==old['node'])
    body=json.dumps(adapter.kanban.by_id[old['task_id']]['body'])
    adapter.kanban.archive(slug,old['task_id'])
    fresh=adapter.kanban.create_node(slug,node,old['idempotency_key'],[],body,enable_workers=False)
    current={**old,'task_id':fresh}
    value['kanban']['task_map']=[current if c['node']==old['node'] else c for c in value['kanban']['task_map']]
    adapter.workflow._write(value)
    ref=adapter.execution_failure({'workflow_id':value['workflow_id'],'node':old['node'],'task_id':fresh},
        'DISPATCH_ARTIFACT_CHANGED','canonical dispatcher binding differs; trusted repair required')
    original_snapshot=adapter.kanban.task_snapshot
    def snapshot(board,tid):
        result=original_snapshot(board,tid)
        if tid==old['task_id']:
            result['runs']=[{'id':1,'profile':'ingest-program','outcome':'scheduled',
                'started_at':1,'ended_at':1,'summary':'Vault workflow cancelled','worker_pid':None}]
            if change=='original_attempted':result['runs'][0]['ended_at']=2
        if tid==fresh and change=='replacement_attempted':result['runs']=[{'id':2,'started_at':2,'ended_at':3}]
        return result
    monkeypatch.setattr(adapter.kanban,'task_snapshot',snapshot)
    path=vault/f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(old['idempotency_key'])}.json"
    if change=='body':adapter.kanban.by_id[old['task_id']]['body']['input_fingerprint']='changed'
    if change=='binding':
        binding=json.loads(path.read_text());binding['actor']='foreign';path.write_text(json.dumps(binding))
    if change=='reason':
        failure=json.loads((vault/ref).read_text());failure['reason']='unknown';(vault/ref).write_text(json.dumps(failure))
    request.update(recovery_id='cancelled-projection-recovery',entries=[{'node':old['node'],'task_id':fresh,
        'failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest()}])
    request['input_digest']=mutation_digest(request)
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if change!='valid':
        with pytest.raises(ContractError,match='STALE_INPUT|RECOVERY_NOT_ALLOWED'):
            adapter.recover_reduce_execution(request)
        assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
        return
    assert not adapter.recover_reduce_execution(request,dry_run=True)['applied']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    result=adapter.recover_reduce_execution(request)
    report=json.loads((vault/result['report_ref']).read_text())
    assert report['entries'][0]['cancelled_projection']['canonical_task_id']==old['task_id']
    assert path.read_bytes()==before[path] and not result['resumed']
