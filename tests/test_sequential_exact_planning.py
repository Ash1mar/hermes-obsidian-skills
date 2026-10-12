"""Program-only fixtures for new plans; no real workflow migration or model calls."""
import hashlib
import json
import pytest

from test_p3_knowledge_build import vault, publish_sources
from test_exact_planning import request_for
from test_phase_grouping_dependencies import dependency_fixture
from hermes_source_units import FileKnowledgeBuildService, ContractError
from hermes_source_units.exact_planning import ExactPlanner
from exact_model_budget import model_measurement


def inventory(vault):
    return {p.relative_to(vault).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in vault.rglob('*') if p.is_file()}


def test_sequential_model_budget_reads_more_than_eight_units_without_writes(vault):
    publish_sources(vault,['\n'.join(f'# Clause {i}\nEvidence {i}.\n' for i in range(24))])
    service=FileKnowledgeBuildService(vault)
    request={'actor':'agent','registry_revision':1,'batch_id':'new-plan-only','max_codepoints':100000,
        'sources':[{'resource_id':'resource-1','unit_set_id':service.source._current('resource-1')['unit_set_id']}]}
    before=inventory(vault)
    planner=ExactPlanner(service,request,read_only=True,model_measure=model_measurement(service,'agent',1))
    plan=planner.prepare()
    flattened=[r for t in plan['tasks'] for r in t['target_refs']]
    assert flattened==planner.refs
    assert any(len(t['target_refs'])>8 for t in plan['tasks'])
    assert all(b['fits'] for t in plan['tasks'] for b in
        [planner.budgets[__import__('hermes_source_units').validation.fingerprint({'session':planner.session,'refs':t['target_refs']})]])
    assert inventory(vault)==before


def test_model_budget_counts_required_table_and_indivisible_oversize(vault):
    source,package,_=dependency_fixture(vault)
    service=FileKnowledgeBuildService(vault)
    request={'actor':'agent','registry_revision':1,'batch_id':'required-table-plan','max_codepoints':12000,
        'sources':[{'resource_id':package['window']['core_refs'][0]['unit_ref']['resource_id'],
                    'unit_set_id':package['window']['core_refs'][0]['unit_ref']['unit_set_id']}]}
    before=inventory(vault)
    measure=model_measurement(service,'agent',1)
    assembled=service._assemble_reading(package['window']['core_refs'],'agent',1,service._reader_config())
    projection=service.preview_reading_model_input(package['window']['core_refs'],assembled,'agent',1)
    assert any(m.get('required_dependency') for m in projection['view']['materials'])
    budget=measure(package['window']['core_refs'],assembled)
    assert budget['fits'] and budget['citation']['future_candidate_input_reserve_codepoints']>0
    blocked=ExactPlanner(service,request,read_only=True,model_measure=model_measurement(service,'agent',1,limit=100))
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        blocked.prepare()
    assert inventory(vault)==before


def test_model_budget_checkpoint_rechecks_fingerprint(vault):
    request=request_for(vault); service=FileKnowledgeBuildService(vault)
    budget=model_measurement(service,'agent',1)
    first=ExactPlanner(service,request,model_measure=budget)
    plan=first.prepare()
    saved=next((first.root/'measurements').glob('*.json'))
    data=json.loads(saved.read_text());data['model_budget']['fits']=not data['model_budget']['fits']
    saved.write_text(json.dumps(data))
    resumed=ExactPlanner(FileKnowledgeBuildService(vault),request,model_measure=model_measurement(FileKnowledgeBuildService(vault),'agent',1))
    assert resumed.prepare()==plan and resumed.measured>0
    changed=ExactPlanner(service,request,model_measure=model_measurement(service,'agent',1,limit=28000))
    assert changed.session!=first.session


def test_new_native_plan_pins_budget_without_replanning_existing_batch(vault):
    from test_exact_planning import test_exact_worker_bound_completion_and_gaps
    # Existing native fixture covers bound exact preparation and completion.
    # Pin inheritance itself is checked without starting a real worker.
    request=request_for(vault);service=FileKnowledgeBuildService(vault)
    planner=ExactPlanner(service,request,model_measure=model_measurement(service,'agent',1))
    plan=planner.prepare()
    from hermes_source_units.semantic_budget import budget_policy_pin
    plan['slice_config']=service._slice_config({'semantic_budget_policy':budget_policy_pin()})
    service._exact_planner=planner
    created=service.plan_batch(plan)
    batch=created['batch']
    slices=service.initialize_slices(batch['batch_id'],batch['actor'],batch['revision'])
    assert slices['batch']['slice_config']['semantic_budget_policy']==budget_policy_pin()
    assert slices['batch']['slice_config']['pass_worker_concurrency']==2
    before=inventory(vault)
    assert not service.plan_batch(plan)['created']
    assert inventory(vault)==before
