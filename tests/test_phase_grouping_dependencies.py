"""Isolated program fixtures, not real native semantic acceptance."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_compact_execution import stopped,vault
from test_source_unit_runtime import register
from hermes_source_units import FileSourceUnitService,FileKnowledgeBuildService,ContractError,mutation_digest
from hermes_source_units.semantic_budget import PHASE_BALANCED_POLICY,budget_policy_pin
from hermes_source_units.reading_dependencies import required_reading
import fixed_semantic as fixed


def dependency_fixture(vault):
    bundle=vault/'10_Raw/dependency-bundle';(bundle/'tables').mkdir(parents=True)
    text='# Rule\nRequired duration follows [Table 1](tables/required.md).\n'
    (bundle/'document.md').write_text(text,encoding='utf-8')
    tables=[]
    for name in ('required','associated'):
        data=f'| name | duration |\n| --- | --- |\n| {name} | 2 h |\n'.encode()
        (bundle/f'tables/{name}.md').write_bytes(data)
        tables.append({'id':name,'path':f'tables/{name}.md','sha256':hashlib.sha256(data).hexdigest(),
            'media_type':'text/markdown','pages':[1]})
    outline={'schema_version':'2.0','sections':[{'id':'rule','title':'Rule','level':1,'parent':None,
        'path':['Rule'],'start_line':1,'end_line':2,'pages':[1],
        'assets':['required','associated'],'quality':'pass'}]}
    (bundle/'outline.json').write_text(json.dumps(outline))
    sha=hashlib.sha256(b'isolated source original').hexdigest();register(vault,sha)
    manifest={'schema_version':'2.0','source':{'sha256':sha},'document':{'path':'document.md'},
        'outline':{'path':'outline.json'},'tables':tables,'images':[],
        'governance':{'vault_id':json.loads((vault/'_system/vault.json').read_text())['vault']['id'],
        'document_id':'doc-demo','version_id':'version-demo-1','resource_id':'resource-demo-1'}}
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    source=FileSourceUnitService(vault);prepared=source.prepare_bundle(bundle.relative_to(vault).as_posix())
    built=source.build({'artifact_manifest':prepared['artifact_manifest'],'config':source.config,
        'actor':'builder','expected_revision':0})
    core=[{'unit_ref':u['ref'],'span':None} for u in built['units'] if u['locator']['kind']=='text']
    service=FileKnowledgeBuildService(vault)
    service.plan_batch({'batch_id':'dependency-test','actor':'agent','registry_revision':1,
        'exact_reading_budget':True,'tasks':[{'task_id':'dependency-task','target_refs':core}]})
    service.prepare_batch('dependency-test','agent',1)
    package=service._existing_reading_package(service._task('dependency-task'),'agent',1)
    # Isolated fixture narrows optional context to expose a genuine missing
    # required dependency. Its core and registered source stay unchanged.
    package=copy.deepcopy(package)
    package['materials']=[m for m in package['materials'] if m['role']=='core']
    package['window']['context_refs']=[]
    from hermes_source_units.validation import fingerprint
    package['window']['window_id']=fingerprint({k:v for k,v in package['window'].items() if k not in ('contract','window_id')})
    package['total_codepoints']=sum(len(m['core_text'] or '') for m in package['materials'])
    package['package_id']=fingerprint({k:v for k,v in package.items() if k!='package_id'})
    return service,package


def test_required_registered_table_supplement_preserves_base_and_ignores_association(vault):
    service,package=dependency_fixture(vault);before=copy.deepcopy(package)
    effective,supplement=required_reading(service.source,package)
    assert package==before
    assert len(supplement['required_refs'])==1
    assert effective[-1]['core_text'].endswith('| required | 2 h |\n')
    view=service.model_input('dependency-task',package,persist=True)['view']
    required=[m for m in view['materials'] if m.get('required_dependency')]
    assert len(required)==1 and required[0]['role']=='context' and '2 h' in required[0]['text']
    linked=[a for m in view['materials'] for a in m.get('linked_assets',[])]
    assert next(a for a in linked if a['name']=='required')['content_provided'] is True
    assert next(a for a in linked if a['name']=='associated')['content_provided'] is False
    assert len(list((vault/'_system/knowledge-builds/task-dependency-task/reading-supplements').glob('*.json')))==1
    assert all(r['unit_ref']['resource_id']==package['window']['core_refs'][0]['unit_ref']['resource_id'] for r in supplement['required_refs'])


def test_required_table_keeps_hash_checks_and_closed_authority(vault):
    service,package=dependency_fixture(vault)
    projection=service.model_input('dependency-task',package)
    with pytest.raises(ContractError,match='OUTSIDE_READING_PACKAGE'):
        service._expand_model_ref('manual-unregistered-table',projection['mapping'])
    ref=package['window']['core_refs'][0]['unit_ref']
    path=vault/'_system/sources/artifacts'/ref['resource_id']/ref['artifact_revision']/'tables/required.md'
    path.write_text('changed bytes')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):
        required_reading(service.source,package)


def test_required_table_inspection_is_mandatory_for_new_pass(vault):
    service,package=dependency_fixture(vault)
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    from hermes_source_units.validation import fingerprint
    path=vault/f"_system/knowledge-builds/task-dependency-task/readings/{package['package_id']}.json"
    _write_atomic(path,_json_bytes(package));before=path.read_bytes()
    task=service._task('dependency-task')
    request={'task_id':task['task_id'],'actor':'agent','expected_revision':task['revision'],
        'registry_revision':1,'reading_package_id':package['package_id'],'sequence':0,'pass_kind':'candidate',
        'inspections':[{'source_ref':r,'finding':'isolated program fixture','qa':'usable','qa_note':''} for r in task['target_refs']],
        'candidates':[],'empty_reason':'isolated coverage fixture'}
    with pytest.raises(ContractError,match='INCOMPLETE_COVERAGE'):
        service.record_pass(request,validate_only=True)
    _,supplement=required_reading(service.source,package)
    request['inspections'].extend({'source_ref':r,'finding':'isolated table inspected','qa':'usable','qa_note':''} for r in supplement['required_refs'])
    assert service.record_pass(request,validate_only=True)['validated']
    assert path.read_bytes()==before and not list(path.parent.parent.glob('passes/*.json'))


def grouping_fixture(count=8):
    tasks=[{'task':f't{i}','materials':[{'ref':'m1','role':'core','text_ref':f'x{i}'}],
        'limits':{'context_truncated':True,'omitted_material_count':3,'reason':'bounded context'},
        'next_sequence':0,'continuation':{'action':'candidate_then_citation',
            'candidate':{'candidates':[],'empty_reason':'isolated grouping fixture'}}} for i in range(count)]
    return {'tasks':tasks,'texts':{f'x{i}':'条件与例外。'*800 for i in range(count)},'headings':[]},tasks


def test_phase_balancing_preserves_tasks_materials_and_complete_budget():
    packet,tasks=grouping_fixture();before=copy.deepcopy(packet)
    candidate,records=fixed.phase_balanced_groups(packet,tasks,'candidate',200000,{},PHASE_BALANCED_POLICY)
    citation,cite_records=fixed.phase_balanced_groups(packet,tasks,'citation',200000,{},PHASE_BALANCED_POLICY)
    assert len(citation)<len(candidate)
    for groups,rows in ((candidate,records),(citation,cite_records)):
        assert [t['task'] for g in groups for t in g]==[t['task'] for t in tasks]
        assert max(map(len,groups))-min(map(len,groups))<=1
        assert all(not r['blocking_reasons'] and r['boundary_reasons'] for r in rows)
        assert all(r['correction_reserve_codepoints']>=2048 for r in rows)
    assert packet==before
    changed=copy.deepcopy(tasks[0]);changed['continuation']['candidate']={'candidates':[{'finding':'full revision '*1000}]}
    assert fixed.estimate_pass_output_tokens(packet,[changed],'citation',PHASE_BALANCED_POLICY)>fixed.estimate_pass_output_tokens(packet,[tasks[0]],'citation',PHASE_BALANCED_POLICY)


def test_correction_reserve_covers_bounded_errors_and_whole_images():
    packet,tasks=grouping_fixture(3)
    images={}
    correction={'failures':[{'task':t['task'],'code':'INVALID_SCHEMA',
        'message':fixed.bounded_correction_message('\\'*160,PHASE_BALANCED_POLICY)} for t in tasks]}
    u=fixed.group_usage(packet,tasks,'citation',100000,images,PHASE_BALANCED_POLICY)
    wire=fixed.wire_prompt('citation',fixed.subset(packet,tasks,'citation'),{},correction)
    size=len(json.dumps(wire,ensure_ascii=False,separators=(',',':')))+len(json.dumps(fixed.strict_schema(fixed.slot_schema('citation',tasks)),ensure_ascii=False,separators=(',',':')))
    assert size<=u['input_codepoints']+u['correction_reserve_codepoints']
    tasks[0]['materials'][0]['asset']={'path':'image','media_type':'image/png'}
    images['image']={'url':'data:image/png;base64,AA==','bytes':99999999,'media_type':'image/png'}
    with pytest.raises(ContractError,match='SEMANTIC_IMAGE_OVERSIZE'):
        fixed.phase_balanced_groups(packet,tasks,'citation',100000,images,PHASE_BALANCED_POLICY)


def test_amendment_pins_policy_and_preserves_blocked_ownership(stopped):
    adapter,value,request=stopped
    request=copy.deepcopy(request);request['semantic_budget_policy']=budget_policy_pin();request['input_digest']=mutation_digest(request)
    service=adapter.workflow.knowledge
    held=next(s for s in service._slices(value['batch_id']) if s['state']!='completed' and not s['lease']['worker_id'])
    held['state']='blocked';service._write_slice(held)
    before=service._slice_path(value['batch_id'],held['slice_id']).read_bytes()
    applied=adapter.amend_execution(request)
    assert applied['held_slices_preserved']==1
    assert service._slice_path(value['batch_id'],held['slice_id']).read_bytes()==before
    assert service._batch(value['batch_id'])['slice_config']['semantic_budget_policy']==budget_policy_pin()
    all_ids=[tid for s in service._slices(value['batch_id']) for tid in s['task_ids']]
    assert len(all_ids)==len(set(all_ids))==len(service._batch(value['batch_id'])['task_ids'])


def test_amendment_rejects_unversioned_policy_change(stopped):
    adapter,value,request=stopped
    request=copy.deepcopy(request);request['semantic_budget_policy']={**budget_policy_pin(),'configuration_sha256':'0'*64}
    request['input_digest']=mutation_digest(request)
    with pytest.raises(ContractError,match='STALE_PLAN'):
        adapter.amend_execution(request,dry_run=True)


def test_phase_budget_runs_fixed_submission_and_citation_with_partial_reuse(stopped,vault,monkeypatch):
    from test_fixed_semantic import test_fixed_pass_preserves_partial_and_controls_phases
    test_fixed_pass_preserves_partial_and_controls_phases(stopped,vault,monkeypatch,
        aggregate_rejected=True,citation_changes=True,phase_balanced=True)
