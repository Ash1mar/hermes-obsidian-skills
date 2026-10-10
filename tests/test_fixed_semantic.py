"""Isolated mechanism fixtures; these are not native semantic acceptance."""
import copy
import json

import pytest
from test_compact_execution import (stopped, vault, begin_amended, semantic_draft,
    workflow_request, start_and_pin)
from test_p3_knowledge_build import prepare_layered_reduce_batch, FakeKanban, IngestKanbanAdapter
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
import fixed_semantic as fixed
import fixed_reduce
from program_worker import admission_check


def test_fixed_pass_admission_precedes_lease_and_preserves_runtime_guard(stopped,vault):
    from pathlib import Path
    from hermes_source_units import mutation_digest
    adapter,value,amendment=stopped
    amendment['worker_template']=(Path(fixed.__file__).parents[1]/'references/workers/pass-slice-compact.md').read_text()
    amendment['input_digest']=mutation_digest(amendment)
    assert adapter.amend_execution(amendment)['applied']
    current=adapter.workflow.status(value['workflow_id'])
    adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor=current['actor'],expected_revision=current['revision']))
    current=adapter.workflow.status(current['workflow_id'])
    card=next(c for c in current['kanban']['task_map'] if adapter.workflow.knowledge._slice(
        current['batch_id'],c['node'].split(':')[1])['state']=='ready')
    path=vault/f"_system/ledgers/ingest-workflows/{current['workflow_id']}/bindings/{fingerprint(card['idempotency_key'])}.json"
    binding=json.loads(path.read_text())
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    assert admission_check(adapter,binding)['ok']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match='STALE_INPUT'):
        adapter.worker_check(binding)
    assert adapter.worker_begin(binding)['leased']
    with pytest.raises(ContractError,match='PASS_ADMISSION_WAIT'):
        admission_check(adapter,binding)


def fixture_response(value):
    raw=json.dumps(value)
    return raw,{'origin':'isolated_fixture','response_sha256':fingerprint(raw)}


def test_pass_groups_bound_output_without_dropping_semantic_limits():
    tasks=[{'task':f't{i}','materials':[{'ref':f'm{i}','role':'core','text_ref':f'x{i}'}],
        'limits':{'context_truncated':True,'omitted_material_count':2,'reason':'context budget'},
        'continuation':{'action':'candidate_then_citation'},'next_sequence':0} for i in range(1,9)]
    packet={'tasks':tasks,'texts':{f'x{i}':'条件与例外。'*800 for i in range(1,9)},'headings':[]}
    before=copy.deepcopy(packet)
    groups=fixed.pass_groups(packet,tasks,'candidate',200000)
    assert len(groups)>1 and [t['task'] for g in groups for t in g]==[t['task'] for t in tasks]
    for group in groups:
        assert fixed.estimate_pass_output_tokens(packet,group)<=fixed.OUTPUT_TOKENS-fixed.OUTPUT_RESERVE
        view=fixed.subset(packet,group,'candidate')
        assert all(t['limits']==before['tasks'][0]['limits'] for t in view['tasks'])
        assert all(t['materials']==o['materials'] for t,o in zip(view['tasks'],group))
    assert packet==before
    packet['texts']['x1']='事实。'*30000
    with pytest.raises(ContractError,match='SEMANTIC_OUTPUT_OVERSIZE'):
        fixed.pass_groups(packet,[tasks[0]],'candidate',200000)


def test_pass_admission_holds_live_domain_capacity_without_claiming():
    from types import SimpleNamespace
    slices=[{'lease':{'worker_id':'already-owned'}}]
    knowledge=SimpleNamespace(_slices=lambda b:slices,_batch=lambda b:{'slice_config':{'pass_worker_concurrency':1}})
    adapter=SimpleNamespace(validate_worker_request=lambda *a,**k:None,
        _slice_worker=lambda *a:({'batch_id':'fixture'},{'state':'ready'}),workflow=SimpleNamespace(knowledge=knowledge))
    with pytest.raises(ContractError,match='PASS_ADMISSION_WAIT'):
        admission_check(adapter,{'node':'pass-slice:next','workflow_id':'fixture','task_id':'next'})
    assert slices==[{'lease':{'worker_id':'already-owned'}}]


def test_fixed_pass_preserves_partial_and_controls_phases(stopped,vault,monkeypatch):
    adapter,value,amendment=stopped
    current,begun=begin_amended(adapter,value,amendment)
    binding=begun['worker_request']; workspace=vault/'fixed-worker'; workspace.mkdir()
    (workspace/'worker-request.json').write_text(json.dumps(binding))
    (workspace/'pass-input-descriptor.json').write_text(json.dumps(begun['model_input']))
    monkeypatch.setenv('HERMES_KANBAN_TASK',binding['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    before={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').glob('task-*/passes/*.json')}
    calls=[]
    from hermes_source_units.knowledge_build import FileKnowledgeBuildService
    from hermes_source_units.workflow_guard import _ACTIVE
    original=FileKnowledgeBuildService.expand_model_passes
    expansion_guards=[]
    def expand_outside_lock(self,*args,**kwargs):
        expansion_guards.append(_ACTIVE.get())
        return original(self,*args,**kwargs)
    monkeypatch.setattr(FileKnowledgeBuildService,'expand_model_passes',expand_outside_lock)
    def caller(phase,view,output_schema,correction):
        calls.append(phase)
        assert '"input_id"' not in json.dumps(view) and '"candidate_pass_id"' not in json.dumps(view)
        assert correction is None
        if phase=='candidate':
            drafts=[]
            for task in view['tasks']:
                assert not {'next_sequence','continuation'} & set(task)
                assert 'limits' in task
                draft=semantic_draft({**task,'input_id':'fixture-only','next_sequence':0},task['task'])
                draft.pop('input_id'); draft.pop('sequence'); drafts.append(draft)
            return fixture_response({'drafts':drafts})
        assert phase=='citation'
        return fixture_response({'reviews':[{'task':t['task'],'decision':'confirmed_unchanged',
            'review_note':'Isolated fixture reviewed source conditions.'} for t in view['tasks']]})
    result=fixed.run_pass(adapter,binding,caller)
    assert result['domain_complete'] and calls==['candidate','citation']
    assert expansion_guards and all(g is None for g in expansion_guards)
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert not adapter.workflow.knowledge._slice(current['batch_id'],binding['node'].partition(':')[2])['lease']['worker_id']
    audits=list((vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-calls").glob('*.json'))
    assert len(audits)==2 and all(json.loads(p.read_text())['validated'] for p in audits)
    # No budget overflow call and no automatic stale-lease repair.
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        fixed.model_call(adapter,binding,'candidate',{},lambda *a:pytest.fail('called'),1)
    with pytest.raises(ContractError):
        fixed.model_call(adapter,binding,'candidate',{},lambda *a:pytest.fail('called'),30000)


def test_fixed_reducers_expand_handles_and_recheck_snapshot(vault,monkeypatch):
    service,_,_,_=prepare_layered_reduce_batch(vault,'fixed-reduce')
    adapter=IngestKanbanAdapter(vault,FakeKanban(True),enable_workers=True)
    value=start_and_pin(vault,workflow_request(workflow_id='ingest-fixed',actor='agent',expected_revision=0,
        profile='compact-3',batch_id='fixed-reduce',scope={'source_paths':[],
        'knowledge_selector':'all-current','execution_mode':'auto_full','provider':'skip','pause_after':['checkpoint_1','build_finalize']}))
    value['dispatch_policy']={'mode':'full','slice_ids':[],'selection_digest':None}; adapter.workflow._write(value)
    def sync():
        current=adapter.workflow.status(value['workflow_id'])
        adapter.sync(workflow_request(workflow_id=current['workflow_id'],actor='agent',expected_revision=current['revision']))
    sync()
    for card in adapter.workflow.status(value['workflow_id'])['kanban']['task_map']:
        if card['node'].startswith('pass-slice:'):
            begun=adapter.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
                'task_id':card['task_id'],'worker_id':'fixture'})
            assert adapter.worker_complete(begun['worker_request'])['ok']; sync()
    calls=[]
    def caller(phase,view,output_schema,correction):
        calls.append(phase); assert correction is None
        refs=[c['candidate'] for c in view['candidates']]
        assert 'pass_id' not in json.dumps(view) and 'task_id' not in json.dumps(view)
        if phase=='resource-reduce':
            return fixture_response({'proposals':[{'candidate_refs':refs,'summary':'Pump X evidence',
                'identity_hints':[{'kind':'entity','identity_key':'project-a:pump-x','canonical_name':'Pump X','aliases':[]}],
                'path_hints':['30_Cards/fixed-pump.md']}], 'omitted_candidate_refs':[],'reason':'Fixture grouping'})
        return fixture_response({'pages':[{'candidate_refs':refs,
            'identity':{'kind':'entity','identity_key':'project-a:pump-x','canonical_name':'Pump X','aliases':[]},
            'path':'30_Cards/fixed-pump.md','content':'# Pump X\n\nTransfers coolant; requires filtered fluid.\n'}],
            'omitted_candidate_refs':[],'reason':'Fixture coordination'})
    current=adapter.workflow.status(value['workflow_id'])
    resources=[c for c in current['kanban']['task_map'] if c['node'].startswith('resource-reduce:')]
    for index,card in enumerate(resources):
        row=next(r for r in adapter.kanban.tasks.values() if r['id']==card['task_id'])
        path=row['body']['worker_binding']; binding=json.loads(open(path,encoding='utf-8').read())
        if index==0:
            ref,snapshot=fixed_reduce.prepare(adapter,binding)
            draft=json.loads(caller('resource-reduce',snapshot['view'],{},None)[0]); calls.clear()
            invalid=copy.deepcopy(draft); invalid['proposals'][0]['candidate_refs']=['c999']
            with pytest.raises(ContractError,match='UNRESOLVED_REFERENCE'): fixed_reduce.submit(adapter,binding,ref,invalid)
            original=json.loads((vault/ref).read_text()); tampered=copy.deepcopy(original)
            tampered['view']['candidates'][0]['finding']='altered'
            (vault/ref).write_text(json.dumps(tampered))
            with pytest.raises(ContractError,match='STALE_INPUT'): fixed_reduce.submit(adapter,binding,ref,draft)
            (vault/ref).write_text(json.dumps(original))
        assert fixed.execute(adapter,path,caller)['ok']; sync()
    current=adapter.workflow.status(value['workflow_id'])
    card=next(c for c in current['kanban']['task_map'] if c['node']=='global-reduce')
    row=next(r for r in adapter.kanban.tasks.values() if r['id']==card['task_id'])
    assert fixed.execute(adapter,row['body']['worker_binding'],caller)['ok']; sync()
    assert calls==['resource-reduce','resource-reduce','global-reduce']
    assert service._batch('fixed-reduce')['global_reduction_id']
    assert len(service._batch('fixed-reduce')['run_ids'])==1
    assert not list((vault/'_system/reports/retrieval').glob('*'))
    # The page reviewer receives semantics, never Finalize request identities.
    from program_worker import execute as program_execute
    def binding_path(kind):
        current=adapter.workflow.status(value['workflow_id'])
        card=next(c for c in current['kanban']['task_map'] if c['node']==kind)
        return next(r['body']['worker_binding'] for r in adapter.kanban.tasks.values() if r['id']==card['task_id'])
    assert program_execute(adapter,binding_path('checkpoint-1-validate'))['ok']; sync()
    current=adapter.workflow.status(value['workflow_id'])
    assert current['pause_control']['boundary']=='checkpoint_1'
    assert not any(c['node']=='build-finalize' for c in current['kanban']['task_map'])
    adapter.continue_workflow(workflow_request(workflow_id=current['workflow_id'],actor='agent',
        expected_revision=current['revision'],continue_id='isolated-review',boundary='checkpoint_1',
        evidence_digest=current['pause_control']['evidence_digest'])); sync()
    def reviewer(phase,view,output_schema,correction):
        assert phase=='page-review' and correction is None
        assert view['citation_evidence'] and 'filtered fluid' in view['content']
        assert not {'run_id','page_id','authored_sha256','expected_revision'} & set(view)
        return fixture_response({'decision':'approved','review_note':'Isolated semantic review preserved both source conditions.'})
    assert fixed.execute(adapter,binding_path('build-finalize'),reviewer)['ok']; sync()
    current=adapter.workflow.status(value['workflow_id'])
    assert current['pause_control']['boundary']=='build_finalize'
    assert (vault/'30_Cards/fixed-pump.md').exists()


def test_lean_inputs_keep_permissions_and_select_only_exact_parent(tmp_path,monkeypatch):
    from types import SimpleNamespace
    packet={'contract':'fixture','texts':{'c1':'necessary AND condition','c2':'unrelated '*200},
        'headings':[['Necessary'],['Unrelated']], 'qa':{'q1':['read qualification'],'q2':['other']},
        'tasks':[{'task':'t1','next_sequence':0,'limits':{},'continuation':{'action':'candidate_then_citation'},
        'materials':[{'ref':'m1','text_ref':'c1','heading':0,'qa_ref':'q1','role':'core'}]}]}
    lean=fixed.subset(packet,packet['tasks'],'candidate')
    assert lean['texts']=={'c1':packet['texts']['c1']} and lean['headings']=={'0':['Necessary']}
    assert lean['qa']=={'q1':['read qualification']} and lean['tasks'][0]['materials']==packet['tasks'][0]['materials']
    assert len(json.dumps(lean))<len(json.dumps(packet))/2
    import hashlib
    parent=tmp_path/'30_Cards/prior.md'; parent.parent.mkdir(); parent.write_text('Existing pump: keep AND conditions.')
    identity={'kind':'entity','identity_key':'pump','canonical_name':'Pump','aliases':[]}
    key=fingerprint({'kind':'entity','identity_key':'pump'})
    value={'view':{'candidates':[{'candidate':'c1','finding':'Pump fact'},{'candidate':'c2','finding':'Other fact'}],
        'existing_pages':[{'identity':identity,'path':'30_Cards/prior.md'}]},
        'backend':{'kind':'global-reduce','batch_id':'fixture','actor':'fixture','tasks':[],
            'mapping':{'c1':[{'pass_id':'a'*64,'candidate_id':'a'}],'c2':[{'pass_id':'b'*64,'candidate_id':'b'}]},
            'reductions':[],'identities':{'revision':1},'document_registry_revision':1,
            'existing_pages':{key:{'subject':{**identity,'current_path':'30_Cards/prior.md'},
                'sha256':hashlib.sha256(parent.read_bytes()).hexdigest(),
                'page':{'qa_status':'usable','business_status':'active','visibility':'released'}}}},'input_id':'a'*64}
    binding={'workflow_id':'ingest-fixture','node':'global-reduce','task_id':'fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path),worker_check=lambda b:{'ok':True},worker_complete=lambda b:{'ok':True})
    monkeypatch.setattr(fixed,'canonical_binding',lambda *a,**k:(binding,{}))
    monkeypatch.setattr(fixed_reduce,'prepare',lambda *a:('fixture',value))
    submissions=[]
    def submit(*args):
        submissions.append(fixed_reduce.expand(value,args[-1])); return {'validated':True}
    monkeypatch.setattr(fixed_reduce,'submit',submit)
    phases=[]
    def caller(phase,view,output_schema,correction):
        phases.append(phase); assert correction is None
        if phase=='global-plan':
            assert all('content' not in p for p in view['existing_pages'])
            return fixture_response({'pages':[{'candidate_refs':['c1'],'identity':identity,'path':'30_Cards/prior.md'}],
                'omitted_candidate_refs':['c2'],'reason':'Isolated explicit unrelated omission'})
        assert phase=='page-write' and len(view['candidates'])==1
        assert view['existing_page']['content']==parent.read_text() and view['candidates'][0]['candidate']=='c1'
        return fixture_response({'content':'Existing pump: keep AND conditions. Pump fact.'})
    assert fixed.execute(adapter,'fixture',caller)['ok']
    assert phases==['global-plan','page-write']
    assert submissions[0]['runs'][0]['decisions'][0]['action']=='update'
    parent.write_text('changed after observation')
    with pytest.raises(ContractError,match='STALE_INPUT'):
        fixed_reduce.page_view(adapter,value,{'identity':identity,'path':'30_Cards/prior.md','candidate_refs':['c1']})
