"""Compact evidence round trips and stopped-workflow amendments, on isolated Vaults."""
import copy
import hashlib
import json
import subprocess
import sys

import pytest

from test_p3_knowledge_build import (vault, ROOT, plan_sliced_batch,
    IngestKanbanAdapter, start_and_pin, workflow_request, KNOWLEDGE_CLI, publish_sources,
    FileKnowledgeBuildService)
from test_pass_revision import ArchivedKeyKanban
from hermes_source_units import ContractError
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
from hermes_source_units.validation import fingerprint

TEMPLATE = ROOT/'hermes-obsidian-governed-ingest-orchestrator/references/workers/pass-slice-compact.md'


def sync(adapter, wid):
    value = adapter.workflow.status(wid)
    return adapter.sync(workflow_request(workflow_id=wid, actor='agent', expected_revision=value['revision']))


def draft(service, tid, package_id, sequence):
    task = service._task(tid)
    return {'task_id':tid,'actor':'agent','expected_revision':task['revision'], 'registry_revision':1,
        'reading_package_id':package_id,'sequence':sequence,'pass_kind':'candidate' if sequence==0 else 'citation',
        'inspections':[{'source_ref':task['target_refs'][0],'finding':'Read source.','qa':'usable','qa_note':''}],
        'candidates':[{'candidate_id':'fact','name':'Source fact','kind':'fact','identity_rationale':'source identity',
            'finding':'Source fact.','applicability':'source','conditions':['Preserve AND/OR.'],
            'exceptions':[],'support_refs':[task['target_refs'][0]]}], 'empty_reason':''}


@pytest.fixture
def stopped(vault):
    service = plan_sliced_batch(vault, 12, 'compact-batch')
    fake = ArchivedKeyKanban()
    adapter = IngestKanbanAdapter(vault, fake, enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id='ingest-compact',actor='agent',
        expected_revision=0,profile='compact-3',batch_id='compact-batch',scope={
            'source_paths':[],'knowledge_selector':'all-current','execution_mode':'auto_full','pause_after':['pass']}))
    value['dispatch_policy']={'mode':'full','slice_ids':[],'selection_digest':None}
    adapter.workflow._write(value)
    sync(adapter,value['workflow_id'])
    value=adapter.workflow.status(value['workflow_id'])
    for index,card in enumerate(value['kanban']['task_map'][:2]):
        begun=adapter.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
            'task_id':card['task_id'],'worker_id':f'old-{index}'})
        bound=begun['worker_request']
        for descriptor in begun['reading_packages'][:None if index==0 else 1]:
            for sequence in ([0,1] if index==0 else [0]):
                with worker_binding(bound),workflow_write_guard(vault,kinds=('pass-slice',)):
                    result=service.record_pass_batch({'batch_id':value['batch_id'],'passes':[
                        draft(service,descriptor['task_id'],descriptor['reading_package_id'],sequence)]})
                    assert result['ok']
        if index==0:
            adapter.worker_complete(bound)
    sync(adapter,value['workflow_id'])
    value=adapter.workflow.status(value['workflow_id'])
    adapter.cancel(workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=value['revision']))
    value=adapter.workflow.status(value['workflow_id'])
    request=workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=value['revision'],
        amendment_id='compact-first',worker_template=TEMPLATE.read_text(encoding='utf-8'),
        reason='Use reversible model inputs and preserve accepted results.',max_tasks=6,max_input_codepoints=30000)
    return adapter,value,request


def semantic_draft(view, alias='t1', sequence=None):
    sequence=view['next_sequence'] if sequence is None else sequence
    ref=next(m['ref'] for m in view['materials'] if m['role']=='core')
    return {'task':alias,'input_id':view['input_id'],'sequence':sequence,
        'inspections':[{'source_ref':ref,'finding':'Read source.','qa':'usable','qa_note':''}],
        'candidates':[{'candidate_id':'fact','name':'Source fact','kind':'fact','identity_rationale':'source identity',
            'finding':'Source fact.','applicability':'source','conditions':['Preserve AND/OR.'],
            'exceptions':[],'support_refs':[ref]}], 'empty_reason':''}


def begin_amended(adapter,value,request):
    assert adapter.amend_execution(request)['applied']
    current=adapter.workflow.status(value['workflow_id'])
    adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=adapter.workflow.status(current['workflow_id'])
    service=adapter.workflow.knowledge
    card=next(c for c in current['kanban']['task_map'] if
        service._slice(current['batch_id'],c['node'].partition(':')[2])['state']!='completed')
    begun=adapter.worker_begin({'workflow_id':current['workflow_id'],'node':card['node'],
        'task_id':card['task_id'],'worker_id':'compact-worker'})
    return current,begun


def test_dry_run_apply_preserves_results_and_completed_native_identity(stopped,vault):
    adapter,value,request=stopped
    old_files={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json')}
    old_task_ids=adapter.workflow.knowledge._batch(value['batch_id'])['task_ids']
    old_completed=next(s for s in adapter.workflow.knowledge._slices(value['batch_id']) if s['state']=='completed')
    binding=next(c for c in value['kanban']['task_map'] if c['node']=='pass-slice:'+old_completed['slice_id'])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    preview=adapter.amend_execution(request,dry_run=True)
    assert not preview['applied'] and preview['new_slice_count']<preview['old_slice_count']
    assert {p:p.read_bytes() for p in vault.rglob('*.json')}==before
    applied=adapter.amend_execution(request)
    assert applied['partial_task_ids'] and applied['completed_slices_preserved']==1
    current=adapter.workflow.status(value['workflow_id'])
    assert current['state']=='cancelled' and current.get('pause_control')==value.get('pause_control')
    assert current['dispatch_policy']==value['dispatch_policy']
    assert binding in current['kanban']['task_map']
    assert adapter.workflow.knowledge._batch(value['batch_id'])['task_ids']==old_task_ids
    assert all(p.read_bytes()==data for p,data in old_files.items())
    assert adapter.amend_execution(request)['revision']==current['revision']
    changed=copy.deepcopy(request);changed['reason']='different';changed['input_digest']=fingerprint({})
    with pytest.raises(ContractError): adapter.amend_execution(changed)
    resumed,begun=begin_amended(adapter,value,request)
    assert binding in resumed['kanban']['task_map']
    assert 'task_snapshots' not in begun and 'reading_packages' not in begun
    assert len(begun['model_inputs'])==6
    assert all(p.read_bytes()==data for p,data in old_files.items())
    for descriptor in begun['model_inputs']:
        view=json.loads((vault/descriptor['path']).read_text())
        assert 'unit_ref' not in json.dumps(view) and 'sha256' not in json.dumps(view)


def test_compact_round_trip_preflight_idempotent_write_and_partial_resume(stopped,vault):
    adapter,value,request=stopped
    current,begun=begin_amended(adapter,value,request)
    bound=begun['worker_request'];service=adapter.workflow.knowledge
    checked=adapter.worker_check(bound)
    descriptor=begun['model_inputs'][0]
    view=json.loads((vault/descriptor['path']).read_text())
    assert view['next_sequence']==1 and view['previous_passes'][0]['sequence']==0
    model={'passes':[semantic_draft(view)]}
    with worker_binding(bound),workflow_write_guard(vault,kinds=('pass-slice',)):
        expanded=service.expand_model_passes(model,checked['task_ids'],'agent',current['batch_id'])
        assert expanded['passes'][0]['sequence']==1
        old={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json')}
        payload={**expanded,'slice_id':checked['slice_id'],'worker_id':bound['worker_id'],'template_hash':bound['template_hash']}
        preview=service.record_pass_batch(payload,validate_only=True)
        assert preview['ok'] and all(p.read_bytes()==data for p,data in old.items())
        result=service.record_pass_batch(payload)
        assert result['ok'] and result['results'][0]['pass_id']==preview['results'][0]['pass_id']
        replay=service.expand_model_passes(model,checked['task_ids'],'agent',current['batch_id'])
        assert service.record_pass_batch({**payload,'passes':replay['passes']})['results'][0]['created'] is False
    prior=service._pass_index([checked['task_ids'][0]])[1][checked['task_ids'][0]]
    assert [p['sequence'] for p in prior]==[0,1]
    assert prior[1]['candidates'][0]['conditions']==['Preserve AND/OR.']
    assert prior[1]['candidates'][0]['support_refs']==prior[0]['candidates'][0]['support_refs']


@pytest.mark.parametrize('change,code',[('handle','OUTSIDE_READING_PACKAGE'),('input','STALE_INPUT'),
    ('task','INVALID_SCHEMA'),('range','INVALID_RANGE')])
def test_compact_rejects_foreign_handles_and_identity(stopped,vault,change,code):
    adapter,value,request=stopped
    current,begun=begin_amended(adapter,value,request)
    bound=begun['worker_request'];checked=adapter.worker_check(bound)
    view=json.loads((vault/begun['model_inputs'][0]['path']).read_text())
    model={'passes':[semantic_draft(view)]}
    if change=='handle':model['passes'][0]['candidates'][0]['support_refs']=['m999']
    if change=='input':model['passes'][0]['input_id']='foreign'
    if change=='task':model['passes'][0]['task']='t999'
    if change=='range':model['passes'][0]['inspections'][0]['source_ref']={'ref':'m1','span':{'start':1,'end':0}}
    before={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json')}
    with pytest.raises(ContractError,match=code):
        adapter.workflow.knowledge.expand_model_passes(model,checked['task_ids'],'agent',current['batch_id'])
    assert all(p.read_bytes()==data for p,data in before.items())


def test_interrupted_amendment_blocks_resume_and_recovers_same_request(stopped,vault,monkeypatch):
    adapter,value,request=stopped
    original=adapter.workflow._write
    def crash(value):
        if value.get('execution_plan'):raise OSError('simulated interruption')
        return original(value)
    monkeypatch.setattr(adapter.workflow,'_write',crash)
    with pytest.raises(OSError,match='interruption'):adapter.amend_execution(request)
    current=adapter.workflow.status(value['workflow_id'])
    with pytest.raises(ContractError,match='EXECUTION_PLAN_PENDING'):
        adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor='agent',expected_revision=current['revision']))
    monkeypatch.setattr(adapter.workflow,'_write',original)
    assert adapter.amend_execution(request)['applied']
    assert not adapter.workflow._execution_journal(value['workflow_id']).exists()
    assert adapter.workflow.status(value['workflow_id'])['state']=='cancelled'


def test_live_source_change_and_worker_cannot_amend(stopped,vault,monkeypatch):
    adapter,value,request=stopped
    monkeypatch.setenv('HERMES_KANBAN_TASK','foreign-worker')
    with pytest.raises(ContractError,match='ACCESS_DENIED'):adapter.amend_execution(request)
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    task=adapter.workflow.knowledge._task(adapter.workflow.knowledge._batch(value['batch_id'])['task_ids'][0])
    ref=task['target_refs'][0]['unit_ref']
    path=vault/'_system/sources/artifacts'/ref['resource_id']/ref['artifact_revision']/'document.md'
    path.write_text('changed source',encoding='utf-8')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):adapter.amend_execution(request,dry_run=True)


def test_real_cli_compact_receipt_does_not_echo_batch_or_mapping(stopped,vault):
    adapter,value,request=stopped
    current,begun=begin_amended(adapter,value,request)
    binding=vault/'binding.json';binding.write_text(json.dumps(begun['worker_request']))
    view=json.loads((vault/begun['model_inputs'][0]['path']).read_text())
    draft_path=vault/'draft.json';draft_path.write_text(json.dumps({'passes':[semantic_draft(view)]}))
    result=subprocess.run([sys.executable,'-X','utf8',str(KNOWLEDGE_CLI),'--vault',str(vault),
        '--worker-binding',str(binding),'batch-pass','--compact','--validate-only','--request',str(draft_path)],
        text=True,capture_output=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr
    receipt=json.loads(result.stdout)
    assert receipt['validated'] and 'batch' not in receipt and 'unit_ref' not in result.stdout
    assert receipt['results'][0]['task']=='t1'


def test_amended_pass_finishes_once_and_holds_reduce(stopped,vault):
    adapter,value,request=stopped
    current,begun=begin_amended(adapter,value,request)
    service=adapter.workflow.knowledge
    old_completed=next(s for s in service._slices(current['batch_id']) if s['state']=='completed')
    preserved={ref:(vault/ref).read_bytes() for ref in old_completed['result_refs']}
    for ordinal in range(2):
        bound=begun['worker_request'];checked=adapter.worker_check(bound)
        views=[json.loads((vault/d['path']).read_text()) for d in begun['model_inputs']]
        with worker_binding(bound),workflow_write_guard(vault,kinds=('pass-slice',)):
            for sequence in (0,1):
                drafts=[semantic_draft(view,f't{i}',sequence) for i,view in enumerate(views,1)
                        if view['next_sequence']<=sequence]
                if drafts:
                    payload={**service.expand_model_passes({'passes':drafts},checked['task_ids'],'agent',current['batch_id']),
                        'slice_id':checked['slice_id'],'worker_id':bound['worker_id'],'template_hash':bound['template_hash']}
                    preview=service.record_pass_batch(payload,validate_only=True)
                    result=service.record_pass_batch(payload)
                    assert result['ok'] and preview['ok']
                    assert [r['pass_id'] for r in result['results']]==[r['pass_id'] for r in preview['results']]
        actual=sum(len((vault/d['path']).read_text(encoding='utf-8')) for d in begun['model_inputs'])
        actual+=len(json.dumps(begun,ensure_ascii=False,indent=2))+1
        assert actual==begun['input_codepoints']<=request['max_input_codepoints']
        adapter.worker_complete(bound)
        sync(adapter,current['workflow_id'])
        current=adapter.workflow.status(current['workflow_id'])
        pending=next((c for c in current['kanban']['task_map'] if c['node'].startswith('pass-slice:')
                      and service._slice(current['batch_id'],c['node'].partition(':')[2])['state']!='completed'),None)
        if pending:
            begun=adapter.worker_begin({'workflow_id':current['workflow_id'],'node':pending['node'],
                'task_id':pending['task_id'],'worker_id':f'compact-{ordinal}'})
    assert current['pause_control']['boundary']=='pass'
    assert current['current_stage']=='analyzing'
    assert all(s['state']=='completed' for s in service._slices(current['batch_id']))
    assert not service._batch(current['batch_id']).get('resource_reduction_ids')
    assert all((vault/ref).read_bytes()==data for ref,data in preserved.items())
    assert sum(len(list((vault/f'_system/knowledge-builds/task-{tid}/passes').glob('*.json')))
               for tid in service._batch(current['batch_id'])['task_ids'])==24
    # Observation and resume do not release the newly reached durable pause.
    adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor='agent',expected_revision=current['revision']))
    assert adapter.workflow.status(current['workflow_id'])['pause_control']['boundary']=='pass'


@pytest.mark.parametrize('active',['attempt','ready-card'])
def test_native_consumer_prevents_amendment(stopped,monkeypatch,active):
    adapter,value,request=stopped
    original=adapter.kanban.task_snapshot
    def snapshot(*args):
        result=original(*args)
        if active=='attempt':result['runs']=[{'status':'running'}]
        else:result['task']['status']='ready'
        return result
    monkeypatch.setattr(adapter.kanban,'task_snapshot',snapshot)
    with pytest.raises(ContractError,match='WORKER_ACTIVE'):
        adapter.amend_execution(request,dry_run=True)
    assert not adapter.workflow._execution_journal(value['workflow_id']).exists()


@pytest.mark.parametrize('tamper',['journal','evidence','completed-evidence'])
def test_interrupted_amendment_rejects_changed_recovery_inputs(stopped,vault,monkeypatch,tamper):
    adapter,value,request=stopped
    original=adapter.workflow._write
    def crash(candidate):
        if candidate.get('execution_plan'):raise OSError('simulated interruption')
        return original(candidate)
    monkeypatch.setattr(adapter.workflow,'_write',crash)
    with pytest.raises(OSError):adapter.amend_execution(request)
    monkeypatch.setattr(adapter.workflow,'_write',original)
    journal=adapter.workflow._execution_journal(value['workflow_id'])
    candidate=json.loads(journal.read_text())
    if tamper=='journal':
        candidate['workflow']['scope']['pause_after']=[]
        journal.write_text(json.dumps(candidate),encoding='utf-8')
    else:
        if tamper=='completed-evidence':
            sid=candidate['manifest']['preserved_slice_ids'][0]
            tid=adapter.workflow.knowledge._slice(value['batch_id'],sid)['task_ids'][0]
        else:
            tid=next(iter(candidate['manifest']['slice_inputs'].values()))[0]['task_id']
        task=adapter.workflow.knowledge._task(tid)
        ref=task['target_refs'][0]['unit_ref']
        path=vault/'_system/sources/artifacts'/ref['resource_id']/ref['artifact_revision']/'document.md'
        path.write_text('changed after interruption',encoding='utf-8')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'):
        adapter.amend_execution(request)
    assert journal.exists() and not adapter.workflow.status(value['workflow_id']).get('execution_plan')


def test_projection_preserves_protected_text_headings_roles_and_actual_qa(vault):
    text='# 条件 🧪\n```text\n'+'A AND B OR C; except D. 单位 10 mm。\n'*70+'```\n'
    refs=publish_sources(vault,[text])
    service=FileKnowledgeBuildService(vault)
    service.plan_batch({'batch_id':'qa-model','actor':'agent','registry_revision':1,
        'exact_reading_budget':True,'tasks':[{'task_id':'qa-model-task','target_refs':refs}]})
    prepared=service.prepare_batch('qa-model','agent',1)
    assert prepared['ok']
    package=service._existing_reading_package(service._task('qa-model-task'),'agent',1)
    projection=service.model_input('qa-model-task',package,persist=True)
    view=projection['view']
    assert [m['text'] for m in view['materials']]==[m['core_text'] for m in package['materials']]
    assert [m['role'] for m in view['materials']]==[m['role'] for m in package['materials']]
    assert any('条件 🧪' in heading for heading in view['headings'])
    restrictions=[q for m in view['materials'] for q in m.get('qa_restrictions',[])]
    assert any(q.get('code')=='oversized-protected-structure' for q in restrictions)
    assert len((vault/projection['path']).read_text(encoding='utf-8'))==projection['codepoints']
    assert (vault/projection['path']).with_suffix('.manifest.json').exists()
    for material in view['materials']:
        ref=service._expand_model_ref(material['ref'],projection['mapping'])
        assert ref==next(m['source_ref'] for m in package['materials'] if m['source_ref']==ref)


def test_prepare_helper_saves_reusable_request_without_vault_changes(stopped,vault,tmp_path):
    adapter,value,request=stopped
    helper=ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/prepare_execution_amendment.py'
    output=tmp_path/'operator-request.json'
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    command=[sys.executable,'-I','-S','-X','utf8',str(helper),'--vault',str(vault),
        '--workflow-id',value['workflow_id'],'--amendment-id','helper-preview','--reason','pack remaining tasks',
        '--output',str(output)]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr
    payload=json.loads(output.read_text(encoding='utf-8'))
    assert payload['expected_revision']==value['revision'] and payload['max_tasks']==6
    assert payload['worker_template']==TEMPLATE.read_text(encoding='utf-8')
    assert adapter.amend_execution(payload,dry_run=True)['ok']
    assert {p:p.read_bytes() for p in vault.rglob('*.json')}==before
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==2 and 'reuse an existing request on retry' in result.stderr
    assert json.loads(output.read_text(encoding='utf-8'))==payload


def test_whole_binary_asset_is_readable_without_expanding_linked_asset_permission(vault):
    from test_source_unit_runtime import register
    from hermes_source_units import FileSourceUnitService
    bundle=vault/'10_Raw/binary-bundle'
    (bundle/'images').mkdir(parents=True)
    data=b'opaque image fixture'
    (bundle/'images/figure.png').write_bytes(data)
    (bundle/'document.md').write_text('# Figure\nSee figure.\n',encoding='utf-8')
    outline={'schema_version':'2.0','sections':[{'id':'figure','title':'Figure','level':1,'parent':None,
        'path':['Figure'],'start_line':1,'end_line':2,'pages':[1],'assets':['figure-1'],'quality':'pass'}]}
    (bundle/'outline.json').write_text(json.dumps(outline),encoding='utf-8')
    sha=hashlib.sha256(b'fixture original').hexdigest()
    manifest={'schema_version':'2.0','source':{'sha256':sha},'document':{'path':'document.md'},
        'outline':{'path':'outline.json'},'images':[{'id':'figure-1','path':'images/figure.png',
        'sha256':hashlib.sha256(data).hexdigest(),'media_type':'image/png','pages':[1]}],'tables':[],
        'governance':{'vault_id':json.loads((vault/'_system/vault.json').read_text())['vault']['id'],
        'document_id':'doc-demo','version_id':'version-demo-1','resource_id':'resource-demo-1'}}
    (bundle/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    register(vault,sha)
    source=FileSourceUnitService(vault)
    prepared=source.prepare_bundle(bundle.relative_to(vault).as_posix())
    built=source.build({'artifact_manifest':prepared['artifact_manifest'],'config':source.config,
        'actor':'builder','expected_revision':0})
    refs=[{'unit_ref':unit['ref'],'span':None} for unit in built['units']]
    service=FileKnowledgeBuildService(vault)
    service.plan_batch({'batch_id':'binary-input','actor':'agent','registry_revision':1,'exact_reading_budget':True,
        'tasks':[{'task_id':f'binary-{i}','target_refs':[ref]} for i,ref in enumerate(refs)]})
    packages=service.prepare_batch('binary-input','agent',1)['results']
    views=[service.model_input(d['task_id'],service._existing_reading_package(service._task(d['task_id']),'agent',1))['view']
           for d in packages]
    asset=next(m for v in views for m in v['materials'] if m.get('asset'))
    assert asset['text'] is None and (vault/asset['asset']['path']).read_bytes()==data
    assert asset['asset']['media_type']=='image/png'
    linked=next(m for v in views for m in v['materials'] if m.get('linked_assets'))
    assert 'asset' not in linked and all(a['content_provided'] is False for a in linked['linked_assets'])
