"""Actual serialized budget, interrupted resume and invalidation acceptance."""
import json

import pytest

from test_p3_knowledge_build import vault, publish_sources
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units.exact_planning import ExactPlanner


def request_for(vault):
    publish_sources(vault, ['# Evidence\n' + ('Evidence is preserved.\n\n'*400)])
    service = FileKnowledgeBuildService(vault)
    current = service.source._current('resource-1')
    return {'actor':'agent','registry_revision':1,'batch_id':'resumable-budget',
            'max_codepoints':12000,
            'sources':[{'resource_id':'resource-1','unit_set_id':current['unit_set_id']}]}


def test_exact_budget_split_resume_and_commit_without_remeasure(vault, monkeypatch):
    request = request_for(vault)
    service = FileKnowledgeBuildService(vault)
    first = ExactPlanner(service, request)
    original = first._progress
    def interrupt(state, **extra):
        original(state, **extra)
        if first.tasks:
            raise RuntimeError('simulated process restart')
    monkeypatch.setattr(first, '_progress', interrupt)
    with pytest.raises(RuntimeError, match='restart'):
        first.prepare()
    resumed_service = FileKnowledgeBuildService(vault)
    resumed = ExactPlanner(resumed_service, request)
    plan = resumed.prepare()
    assert resumed.reused > 0
    assert sum(len(task['target_refs']) for task in plan['tasks']) == len(resumed.refs)
    assert len({json.dumps(r,sort_keys=True) for t in plan['tasks'] for r in t['target_refs']}) == len(resumed.refs)
    fresh = FileKnowledgeBuildService(vault)
    for task in plan['tasks']:
        assert fresh.measure_reading_window(task['target_refs'],1,None,resumed.reader,actor='agent')['fits']
    second = ExactPlanner(FileKnowledgeBuildService(vault), request)
    assert second.prepare() == plan
    assert second.measured == 0
    resumed_service._exact_planner = resumed
    before = resumed.measured
    # Batch preflight checks the whole live source once; task validation should
    # stay proportional to assigned units as a batch grows.
    import hermes_source_units.knowledge_build as knowledge_module
    validate = knowledge_module.validate_references
    validated_counts = []
    def track_validation(kind, record, units):
        if kind == 'work':
            validated_counts.append(len(units))
        return validate(kind, record, units)
    monkeypatch.setattr(knowledge_module, 'validate_references', track_validation)
    result = resumed_service.plan_batch(plan)
    assert result['created_tasks'] == len(plan['tasks'])
    assert resumed.measured == before
    assert sum(validated_counts) == len(resumed.refs)


def test_measure_checkpoint_detects_corruption_and_changed_artifact(vault):
    request = request_for(vault)
    planner = ExactPlanner(FileKnowledgeBuildService(vault), request)
    plan = planner.prepare()
    saved = next((planner.root/'measurements').glob('*.json'))
    saved.write_text('{broken',encoding='utf-8')
    resumed = ExactPlanner(FileKnowledgeBuildService(vault), request)
    assert resumed.prepare() == plan
    assert resumed.measured > 0
    ref = resumed.refs[0]['unit_ref']
    document = vault/'_system/sources/artifacts'/ref['resource_id']/ref['artifact_revision']/'document.md'
    document.write_text(document.read_text(encoding='utf-8')+'changed',encoding='utf-8')
    with pytest.raises(ContractError, match='SOURCE_CHANGED'):
        ExactPlanner(FileKnowledgeBuildService(vault), request)


def test_full_reference_cache_invalidates_and_acl_remains_live(vault, monkeypatch):
    request = request_for(vault)
    service = FileKnowledgeBuildService(vault).source
    service.enable_session_cache()
    units = service.list('resource-1',request['sources'][0]['unit_set_id'])
    ref = {'unit_ref':units[0]['ref'],'span':None}
    access = {'actor':'agent','purpose':'construction','registry_revision':1}
    assert service.get({'source_ref':ref,'access':access})
    root = vault/'_system/sources/units'/'resource-1'/request['sources'][0]['unit_set_id']
    # Locate the actual immutable repository from its loaded manifest.
    from hermes_source_units.source_units import UNIT_ROOT
    root = vault/UNIT_ROOT/'resource-1'/request['sources'][0]['unit_set_id']
    path = root/'units.jsonl'
    records = path.read_text(encoding='utf-8').splitlines()
    changed = json.loads(records[0]); changed['unexpected'] = True
    records[0] = json.dumps(changed)
    path.write_text('\n'.join(records)+'\n',encoding='utf-8')
    with pytest.raises(ContractError):
        service.get({'source_ref':ref,'access':access})


def test_snapshot_rejects_change_before_result_escapes(vault):
    request = request_for(vault)
    service = FileKnowledgeBuildService(vault).source
    units = service.list('resource-1',request['sources'][0]['unit_set_id'])
    ref = {'unit_ref':units[0]['ref'],'span':None}
    access = {'actor':'agent','purpose':'construction','registry_revision':1}
    with pytest.raises(ContractError, match='SOURCE_CHANGED'):
        with service.reading_snapshot():
            service.get({'source_ref':ref,'access':access})
            registry = vault/'_system/metadata/document-registry.json'
            registry.write_text(registry.read_text(encoding='utf-8')+' ',encoding='utf-8')


def test_exact_worker_bound_completion_and_gaps(vault):
    from test_p3_knowledge_build import (FakeKanban,IngestKanbanAdapter,
        start_and_pin,workflow_request)
    from exact_preparation import prepare_exact
    refs = publish_sources(vault,['# A\nSource A.\n','# B\nSource B.\n'])
    fake = FakeKanban(True)
    adapter = IngestKanbanAdapter(vault,fake,enable_workers=True)
    started = start_and_pin(vault,workflow_request(workflow_id='ingest-exact-worker',
        actor='agent',expected_revision=0,profile='compact-3',
        scope={'source_paths':['10_Raw/source-1.md','10_Raw/source-2.md'],
               'knowledge_selector':'fixed batch_id exact-worker-batch',
               'execution_mode':'canary_only'}))
    adapter._sync_current(started['workflow_id'])
    cards = adapter.workflow.status(started['workflow_id'])['kanban']['task_map']
    for card in cards:
        if not card['node'].startswith('source-prepare:'):
            continue
        req = {'workflow_id':started['workflow_id'],'node':card['node'],'task_id':card['task_id']}
        begun = adapter.worker_begin(req)
        selected = refs[0 if begun['source']['path'].endswith('source-1.md') else 1]['unit_ref']
        adapter.worker_complete({**req,'template_hash':begun['template_hash'],
            'resource_id':selected['resource_id'],'unit_set_id':selected['unit_set_id']})
    adapter._sync_current(started['workflow_id'])
    card = next(item for item in adapter.workflow.status(started['workflow_id'])['kanban']['task_map']
                if item['node']=='exact-plan')
    result = prepare_exact(adapter,{'workflow_id':started['workflow_id'],
        'node':card['node'],'task_id':card['task_id']})
    assert result['batch_id']=='exact-worker-batch'
    assert adapter.workflow.status(started['workflow_id'])['current_stage']=='analyzing'


def test_many_context_candidates_do_not_make_tiny_core_unreadable(vault):
    request = request_for(vault)
    service = FileKnowledgeBuildService(vault)
    refs = service.source.list('resource-1',request['sources'][0]['unit_set_id'])
    access = {'actor':'agent','purpose':'construction','registry_revision':1}
    first = {'unit_ref':refs[0]['ref'],'span':None}
    bounded = service.source.context({'core_refs':[first],'access':access,
        'max_codepoints':12000,'max_context_units':8})
    assert len(bounded['context'])+len(bounded['omitted_refs']) <= 8
    planner = ExactPlanner(service,request)
    plan = planner.prepare()
    assert sum(len(task['target_refs']) for task in plan['tasks']) == len(refs)
