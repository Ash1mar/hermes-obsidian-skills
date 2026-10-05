"""Semantic revisions retain immutable history and cannot release Pass pauses."""
import copy
import json
import hashlib
import pytest
from test_p3_knowledge_build import (vault, ROOT, plan_sliced_batch, FakeKanban,
    IngestKanbanAdapter, start_and_pin, workflow_request)
from hermes_source_units import ContractError
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


def sync(adapter, wid):
    value = adapter.workflow.status(wid)
    return adapter.sync(workflow_request(workflow_id=wid, actor='agent', expected_revision=value['revision']))


@pytest.fixture
def paused(vault):
    batch = plan_sliced_batch(vault, 3, 'revision-batch')
    adapter = IngestKanbanAdapter(vault, FakeKanban(True), enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id='ingest-revisions', actor='agent',
        expected_revision=0, profile='compact-3', batch_id='revision-batch',
        scope={'source_paths': [], 'knowledge_selector': 'all-current', 'execution_mode': 'auto_full',
               'pause_after': ['pass']}))
    value['dispatch_policy'] = {'mode':'full','slice_ids':[],'selection_digest':None}
    adapter.workflow._write(value)
    sync(adapter, value['workflow_id'])
    value = adapter.workflow.status(value['workflow_id'])
    card = value['kanban']['task_map'][0]
    bound = {'workflow_id':value['workflow_id'], 'node':card['node'], 'task_id':card['task_id'], 'worker_id':'first'}
    begun = adapter.worker_begin(bound)
    bound = begun['worker_request']
    for task, package in zip(begun['task_snapshots'], begun['reading_packages']):
        for seq in (0,1):
            draft = {'batch_id': value['batch_id'], 'passes': [{
                'task_id':task['task_id'], 'actor':'agent', 'expected_revision':batch._task(task['task_id'])['revision'],
                'registry_revision':1, 'reading_package_id':package['reading_package_id'],
                'pass_kind':'candidate' if seq == 0 else 'citation', 'sequence':seq,
                'inspections':[{'source_ref':task['target_refs'][0], 'finding':'read core','qa':'usable','qa_note':''}],
                'candidates':[{'candidate_id':'claim','name':'Claim','kind':'fact','identity_rationale':'exact evidence',
                    'finding':'Source claim','applicability':'bounded source','conditions':[],'exceptions':[],
                    'support_refs':[task['target_refs'][0]]}], 'empty_reason':''}]}
            with worker_binding(bound), workflow_write_guard(vault, kinds=('pass-slice',)):
                assert batch.record_pass_batch(draft)['ok']
    adapter.worker_complete(bound)
    sync(adapter, value['workflow_id'])
    value = adapter.workflow.status(value['workflow_id'])
    assert value['pause_control']['boundary'] == 'pass'
    task_id = begun['task_snapshots'][0]['task_id']
    prior = batch._pass_index([task_id])[1][task_id][-1]
    review = vault/'_system/reports/revision-review.json'
    review.write_text(json.dumps({'task_id':task_id,'reason':'claim requires citation review'}))
    request = workflow_request(workflow_id=value['workflow_id'], actor='agent', expected_revision=value['revision'],
        revision_id='review-one', evidence_digest=value['pause_control']['evidence_digest'],
        tasks=[{'task_id':task_id,'pass_id':prior['pass_id'],'reason':'Recheck citation coverage'}],
        reason='Semantic evidence review', evidence_refs=['_system/reports/revision-review.json'],
        worker_template=(ROOT/'hermes-obsidian-governed-ingest-orchestrator/references/pass-revision-worker.md').read_text())
    return adapter, value, request, prior


def test_append_revision_preserves_history_and_waits_for_review(paused, vault):
    adapter, before, request, prior = paused
    old_bytes = {p.relative_to(vault).as_posix():p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json')}
    adapter.revise_passes(request)
    assert adapter.workflow.revise_passes(request)['pass_revision']['state'] == 'running'
    value = adapter.workflow.status(before['workflow_id'])
    assert value['pause_control'] == before['pause_control']
    with pytest.raises(ContractError, match='REVISION_REVIEW_REQUIRED'):
        adapter.continue_workflow(workflow_request(workflow_id=value['workflow_id'], actor='agent',
            expected_revision=value['revision'], continue_id='too-early',boundary='pass',
            evidence_digest=value['pause_control']['evidence_digest']))
    card = value['kanban']['task_map'][0]
    with pytest.raises(ContractError, match='ACCESS_DENIED'):
        adapter.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
            'task_id':before['kanban']['task_map'][0]['task_id'],'worker_id':'stale'})
    begun = adapter.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
        'task_id':card['task_id'],'worker_id':'revision'})
    assert len(begun['task_snapshots']) == 1
    bound = begun['worker_request']
    task = begun['task_snapshots'][0]
    draft_pass = {key:copy.deepcopy(prior[key]) for key in ('task_id','actor','reading_package_id','inspections','candidates','empty_reason')}
    draft_pass.update(expected_revision=task['revision'],registry_revision=1,pass_kind='citation',sequence=2)
    draft_pass['candidates'][0]['finding'] = 'Reviewed source claim'
    draft = {'batch_id':value['batch_id'],'passes':[draft_pass]}
    batch = adapter.workflow.knowledge
    with worker_binding(bound), workflow_write_guard(vault, kinds=('pass-slice',)):
        checked = batch.record_pass_batch(draft, validate_only=True)
        assert checked['ok'] and checked['validated']
        assert all((vault/ref).read_bytes() == data for ref,data in old_bytes.items())
        bad = copy.deepcopy(draft)
        bad['passes'][0]['task_id'] = next(t for t in batch._batch(value['batch_id'])['task_ids'] if t != task['task_id'])
        assert not batch.record_pass_batch(bad, validate_only=True)['ok']
        written = batch.record_pass_batch(draft)
        assert written['ok'] and written['results'][0]['pass_id'] == checked['results'][0]['pass_id']
    adapter.worker_complete(bound)
    result = sync(adapter, value['workflow_id'])
    assert not result['background_dispatch']
    current = adapter.workflow.status(value['workflow_id'])
    assert current['pass_revision']['state'] == 'awaiting_review'
    assert current['pause_control']['boundary'] == 'pass'
    assert current['pause_control']['evidence_digest'] != before['pause_control']['evidence_digest']
    assert all((vault/ref).read_bytes() == data for ref,data in old_bytes.items())
    candidates, records = batch._pass_index([task['task_id']])
    assert [p['sequence'] for p in records[task['task_id']]] == [0,1,2]
    assert (prior['pass_id'],'claim') not in candidates
    new_id = written['results'][0]['pass_id']
    assert (new_id,'claim') in candidates
    accept = workflow_request(workflow_id=current['workflow_id'],actor='agent',
        expected_revision=current['revision'],revision_id='review-one',
        evidence_digest=current['pause_control']['evidence_digest'],evidence_refs=request['evidence_refs'])
    adapter.workflow.accept_pass_revision(accept)
    accepted = adapter.workflow.status(current['workflow_id'])
    assert adapter.workflow.accept_pass_revision(accept)['revision'] == accepted['revision']
    assert accepted['pause_control']['boundary'] == 'pass'
    assert not sync(adapter, current['workflow_id'])['background_dispatch']


@pytest.mark.parametrize('change,code',[('actor','ACTOR_MISMATCH'),('revision','REVISION_CONFLICT'),
    ('pass','STALE_INPUT'),('evidence','STALE_INPUT'),('worker','ACCESS_DENIED')])
def test_revision_authorization_rejects_stale_or_worker_requests(paused, vault, monkeypatch, change, code):
    adapter,value,request,prior = paused
    from hermes_source_units import mutation_digest
    if change == 'actor': request['actor'] = 'foreign'
    if change == 'revision': request['expected_revision'] -= 1
    if change == 'pass': request['tasks'][0]['pass_id'] = '0'*64
    if change == 'evidence':
        (vault/'_system/metadata/source-organizations.json').write_bytes(b'changed')
    if change == 'worker': monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    request['input_digest'] = mutation_digest(request)
    before = adapter.workflow._path(value['workflow_id']).read_bytes()
    with pytest.raises(ContractError, match=code): adapter.revise_passes(request)
    assert adapter.workflow._path(value['workflow_id']).read_bytes() == before
