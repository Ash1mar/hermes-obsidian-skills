"""Reuse the citation revision path while preserving released pause history."""
import hashlib
import json
from pathlib import Path
import pytest
from test_pass_revision import paused,vault,sync
from test_p3_knowledge_build import ROOT,workflow_request
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.reading_dependencies import citation_dependency_gaps
from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
from test_phase_grouping_dependencies import dependency_fixture


def setup_scope(paused,vault,monkeypatch):
    a,w,old_request,prior=paused
    a.continue_workflow(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
        continue_id='open-reduce',boundary='pass',evidence_digest=w['pause_control']['evidence_digest']))
    w=a.workflow.status(w['workflow_id'])
    assert w['current_stage']=='reducing'
    a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
    w=a.workflow.status(w['workflow_id'])
    tid=old_request['tasks'][0]['task_id']
    resource=a.workflow.knowledge._task(tid)['target_refs'][0]['unit_ref']['resource_id']
    card=next(c for c in w['kanban']['task_map'] if c['node']=='resource-reduce:'+resource)
    ref=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},
        'INCOMPLETE_COVERAGE','required original dependency lacks citation inspection')
    monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',lambda *_a,**_k:{'actual_consumers':0})
    monkeypatch.setattr('hermes_source_units.reduce_dependency_revision.citation_dependency_gaps',lambda *_:[{'missing_ref':'isolated gap'}])
    request=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
        revision_id='dependency-revision',revision_scope='reduce-dependencies/v1',
        evidence_digest='sha256:'+fingerprint(w['pause_control']),tasks=old_request['tasks'],
        reduce_failures=[{'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest()}],
        reason='Supply a previously missing required dependency',evidence_refs=old_request['evidence_refs'],
        worker_template=(ROOT/'hermes-obsidian-governed-ingest-orchestrator/references/workers/pass-slice.md').read_text())
    return a,w,request


def test_dependency_revision_keeps_released_pauses_and_waits_for_review(paused,vault,monkeypatch):
    a,w,request=setup_scope(paused,vault,monkeypatch)
    old_passes={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').glob('task-*/passes/*.json')}
    result=a.revise_passes(request)
    current=a.workflow.status(w['workflow_id'])
    assert current['cancel_requested'] and not result['resumed']
    assert current['pause_control']==w['pause_control']
    assert all(p.read_bytes()==b for p,b in old_passes.items())
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    assert all(c['node'].startswith('pass-slice:') for c in current['kanban']['task_map'])
    selected=set(current['pass_revision']['slice_ids'])
    for card in current['kanban']['task_map']:
        if card['node'].partition(':')[2] not in selected:continue
        begun=a.worker_begin({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id'],'worker_id':'isolated'})
        assert begun['model_input']
        packet=json.loads((vault/begun['model_input']['path']).read_text())
        assert packet['tasks'][0]['continuation']['candidate']['revision_review']
        binding=begun['worker_request'];k=a.workflow.knowledge
        for item in current['pass_revision']['tasks']:
            if item['task_id'] not in k._slice(w['batch_id'],card['node'].partition(':')[2])['task_ids']:continue
            prior=json.loads((vault/item['pass_ref']).read_text())
            draft={key:prior[key] for key in ('task_id','actor','reading_package_id','inspections','candidates','empty_reason')}
            draft.update(expected_revision=k._task(item['task_id'])['revision'],registry_revision=1,
                         sequence=item['sequence'],pass_kind='citation')
            with worker_binding(binding),workflow_write_guard(vault,kinds=('pass-slice',),actor='agent'):
                assert k.record_pass_batch({'batch_id':w['batch_id'],'passes':[draft]})['ok']
        assert a.worker_complete(binding)['ok']
    sync(a,w['workflow_id'])
    current=a.workflow.status(w['workflow_id'])
    assert current['pass_revision']['state']=='awaiting_review'
    assert current['pause_control']==w['pause_control']
    assert a.workflow.pending_pause(current)=='revision_review'
    assert all(a.kanban.task_snapshot('',c['task_id'])['task']['status']=='done'
               for c in current['kanban']['task_map'] if c['node'].partition(':')[2] in selected)
    assert all(p.read_bytes()==b for p,b in old_passes.items())
    with pytest.raises(ContractError,match='WORKFLOW_PAUSED'):a.workflow.assert_not_paused(current)


def test_dependency_revision_rejects_unaffected_tasks_and_stale_failure(paused,vault,monkeypatch):
    a,w,request=setup_scope(paused,vault,monkeypatch)
    monkeypatch.setattr('hermes_source_units.reduce_dependency_revision.citation_dependency_gaps',lambda *_:[])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match='unaffected citation'):a.revise_passes(request)
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}


def test_gap_diagnostic_identifies_pass_package_ref_and_trigger_without_inferring_inspection(vault):
    k,package=dependency_fixture(vault)
    record={'task_id':'dependency-task','pass_id':'old-citation','reading_package_id':package['package_id'],
            'inspections':[{'source_ref':ref} for ref in package['window']['core_refs']]}
    gaps=citation_dependency_gaps(k.source,package,record)
    assert len(gaps)==1 and gaps[0]['links'] and not gaps[0]['present_original_package']
    assert gaps[0]['pass_id']=='old-citation' and gaps[0]['reading_package_id']==package['package_id']
    assert gaps[0]['missing_ref']['unit_ref']['resource_id']=='resource-demo-1'
