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
    for key in ('drafts','reviews'):
        if key in value:
            value={'results':{item['task']:{k:v for k,v in item.items() if k!='task'} for item in value[key]}}
            break
    raw=json.dumps(value)
    return raw,{'origin':'isolated_fixture','response_sha256':fingerprint(raw)}


def test_semantic_transport_preserves_strings_and_rejects_extra_content():
    raw='{"review_note":"source literally contains </final>"}'
    for wire in (raw,raw+'</final>','<final>'+raw+'</final>'):
        value,_=fixed.decode_semantic_response(wire)
        assert value=={'review_note':'source literally contains </final>'}
    for wire in (raw+' commentary',raw+'{}',raw+'{}</final>','```json\n'+raw+'\n```','{"t1":{},"t1":{}}'):
        with pytest.raises(json.JSONDecodeError):fixed.decode_semantic_response(wire)


def test_fixed_slots_preserve_valid_reviews_and_target_missing_tasks():
    from fixed_semantic_schema import slot_schema,slot_results,strict_schema
    tasks=[{'task':'t1'},{'task':'t4'}]
    output=slot_schema('citation',tasks)
    assert output['properties']['results']['required']==['t1','t4']
    strict=strict_schema(output)
    assert strict['type']=='object' and strict['additionalProperties'] is False
    unchanged={'decision':'confirmed_unchanged','review_note':'Checked source conditions.'}
    partial=slot_results('citation',{'results':{'t1':unchanged}},tasks)
    assert partial['reviews']==[{'task':'t1',**unchanged}]
    assert [f['task'] for f in partial['slot_failures']]==['t4']
    assert slot_results('citation',{'results':{'t1':unchanged,'t4':unchanged}},tasks)['slot_failures']==[]
    with pytest.raises(ContractError,match='unexpected task slots'):
        slot_results('citation',{'results':{'t2':unchanged}},tasks)
    spoof=slot_results('citation',{'results':{'t1':{**unchanged,'task':'t4'},'t4':unchanged}},tasks)
    assert [r['task'] for r in spoof['reviews']]==['t4']


def test_image_budget_preserves_nine_complete_images_and_reports_actual_limits():
    from fixed_semantic_media import require_images_fit,image_budget
    nine=[{'bytes':35000} for _ in range(9)]
    require_images_fit(nine)
    budget=image_budget()
    with pytest.raises(ContractError,match='image_count'):
        require_images_fit([{'bytes':1} for _ in range(budget['max_images']+1)])
    with pytest.raises(ContractError,match='largest_image_bytes'):
        require_images_fit([{'bytes':budget['max_bytes']+1}])


def test_audited_recovery_requires_same_task_input_and_native_response(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from fixed_semantic_recovery import recover
    home=tmp_path/'isolated-home';home.mkdir()
    wid='isolated-workflow'
    binding={'workflow_id':wid,'task_id':'fresh-native','node':'pass-slice:new','template_hash':'current'}
    task={'task':'t1','materials':[{'ref':'m1','role':'core','text_ref':'c1','heading':0}],
          'candidate':{'candidates':[]},'limits':{}}
    view={'tasks':[task],'texts':{'c1':'Condition AND exception.'},'headings':{'0':['Scope']}}
    user='Native task prior-native\n'+json.dumps({'phase':'citation','input':view})
    raw=json.dumps({'reviews':[{'task':'t1','decision':'confirmed_unchanged','review_note':'Source reviewed.'}]})
    db=sqlite3.connect(home/'state.db')
    db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
    db.execute('INSERT INTO sessions VALUES (?,?)',('fixture','kanban'))
    db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[('fixture',r,c,None) for r,c in [('system','fixed system'),('user',user),('assistant',raw)]])
    db.commit();db.close()
    root=tmp_path/'_system/ledgers/ingest-workflows'/wid/'semantic-calls';root.mkdir(parents=True)
    audit={'workflow_id':wid,'phase':'citation','native_task_id':'prior-native','node':'pass-slice:old',
           'user_sha256':fingerprint(user),'response_sha256':fingerprint(raw),
           'input_sha256':fingerprint({'system':'fixed system','user':user}),
           'model':{'origin':'hermes_native_model','session_id':'fixture','response_sha256':fingerprint(raw)}}
    (root/'old.json').write_text(json.dumps(audit))
    adapter=SimpleNamespace(worker_check=lambda b:{'batch_id':'b','task_ids':['same-task']},
        workflow=SimpleNamespace(vault=tmp_path,knowledge=SimpleNamespace(_slice=lambda *a:{'task_ids':['same-task']})))
    result= recover(adapter,binding,'citation',view,fixed.decode_semantic_response,'fixed system',home=home)
    assert result[0]['reviews'][0]['task']=='t1'
    changed=copy.deepcopy(view);changed['texts']['c1']='Changed OR scope.'
    assert recover(adapter,binding,'citation',changed,fixed.decode_semantic_response,'fixed system',home=home) is None
    assert recover(adapter,binding,'citation',view,fixed.decode_semantic_response,'changed instructions',home=home) is None
    audit['response_sha256']=fingerprint('tampered')
    (root/'old.json').write_text(json.dumps(audit))
    assert recover(adapter,binding,'citation',view,fixed.decode_semantic_response,'fixed system',home=home) is None


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


@pytest.mark.parametrize('aggregate_rejected',[False,True])
@pytest.mark.parametrize('citation_changes',[False,True])
def test_fixed_pass_preserves_partial_and_controls_phases(stopped,vault,monkeypatch,aggregate_rejected,citation_changes):
    from pathlib import Path
    from hermes_source_units import mutation_digest
    adapter,value,amendment=stopped
    amendment['worker_template']=(Path(fixed.__file__).parents[1]/'references/workers/pass-slice-compact.md').read_text()
    amendment['input_digest']=mutation_digest(amendment)
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
    import native_ingest
    # The complete checked program packet may exceed a human message budget.
    # Human workers still fail that bound; program calls group before exposure.
    monkeypatch.setattr(native_ingest,'present_model_packet',lambda packet:'large human presentation '*20000)
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        native_ingest.read_pass_input(vault)
    original_action=fixed.worker_action
    rejected=[]
    def action(vault,action,payload=None):
        if aggregate_rejected and action=='submit' and not rejected:
            rejected.append(True)
            return {'ok':False,'validated':False,'state':'draft_rejected','code':'INVALID_SCHEMA',
                'error':'isolated aggregate schema rejection','next_action':'correct_semantic_draft'}
        return original_action(vault,action,payload)
    monkeypatch.setattr(fixed,'worker_action',action)
    def caller(phase,view,output_schema,correction):
        calls.append(phase)
        assert '"input_id"' not in json.dumps(view) and '"candidate_pass_id"' not in json.dumps(view)
        if aggregate_rejected and calls==['candidate','candidate']:
            assert correction['code']=='INVALID_SCHEMA' and 'aggregate schema rejection' in correction['message']
        else: assert correction is None
        if phase=='candidate':
            drafts=[]
            for task in view['tasks']:
                assert not {'next_sequence','continuation'} & set(task)
                assert 'limits' in task
                draft=semantic_draft({**task,'input_id':'fixture-only','next_sequence':0},task['task'])
                draft.pop('input_id'); draft.pop('sequence'); drafts.append(draft)
            return fixture_response({'drafts':drafts})
        assert phase=='citation'
        if citation_changes:
            reviews=[]
            for task in view['tasks']:
                candidate=copy.deepcopy(task['candidate']['candidates'][0])
                candidate['finding']+=' (source fidelity checked)'
                reviews.append({'task':task['task'],'decision':'revised','review_note':'Checked conditions and support.',
                    'candidates':[candidate],'removed_candidate_ids':[],'inspections':[],'empty_reason':''})
            return fixture_response({'reviews':reviews})
        return fixture_response({'reviews':[{'task':t['task'],'decision':'confirmed_unchanged',
            'review_note':'Isolated fixture reviewed source conditions.'} for t in view['tasks']]})
    if not aggregate_rejected and not citation_changes:
        original_caller=caller
        class NativeFixtureCaller(fixed.HermesCaller):
            def __init__(self): pass
            def __call__(self,*args): return original_caller(*args)
        caller=NativeFixtureCaller()
        import fixed_semantic_recovery
        def verify_live_recovery_binding(adapter,actual,*args):
            assert actual==json.loads((workspace/'worker-request.json').read_text())
            assert adapter.worker_check(actual)['ok']
            return None
        monkeypatch.setattr(fixed_semantic_recovery,'recover',verify_live_recovery_binding)
    result=fixed.run_pass(adapter,binding,caller)
    assert result['domain_complete'] and calls==(['candidate','candidate','citation'] if aggregate_rejected else ['candidate','citation'])
    assert expansion_guards and all(g is None for g in expansion_guards)
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert not adapter.workflow.knowledge._slice(current['batch_id'],binding['node'].partition(':')[2])['lease']['worker_id']
    audits=[json.loads(p.read_text()) for p in (vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-calls").glob('*.json')]
    assert len(audits)==(3 if aggregate_rejected else 2)
    assert sum(a.get('validated',False) for a in audits)==2
    if citation_changes:
        for task_id in adapter.workflow.knowledge._slice(current['batch_id'],binding['node'].partition(':')[2])['task_ids']:
            paths=sorted((vault/f'_system/knowledge-builds/task-{task_id}/passes').glob('*.json'))
            records=[json.loads(p.read_text()) for p in paths]
            assert records[1]['inspections']==records[0]['inspections']
            assert records[1]['candidates'][0]['finding'].endswith('(source fidelity checked)')
            assert records[1]['candidates'][0]['conditions']==records[0]['candidates'][0]['conditions']
            assert records[1]['candidates'][1:]==records[0]['candidates'][1:]
    if aggregate_rejected:
        rejection=next(a for a in audits if not a['validated'])
        assert len(rejection['receipt_refs'])==1
        receipt=json.loads((vault/rejection['receipt_refs'][0]).read_text())
        assert receipt['code']=='INVALID_SCHEMA' and receipt['state']=='draft_rejected'
    # No budget overflow call and no automatic stale-lease repair.
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        fixed.model_call(adapter,binding,'candidate',{'tasks':[]},lambda *a:pytest.fail('called'),1)
    with pytest.raises(ContractError):
        fixed.model_call(adapter,binding,'candidate',{'tasks':[]},lambda *a:pytest.fail('called'),30000)


@pytest.mark.parametrize('live',[False,True])
def test_trusted_termination_settles_only_ended_current_lease(stopped,vault,monkeypatch,live):
    from datetime import datetime
    from pathlib import Path
    from hermes_source_units import mutation_digest
    adapter,value,amendment=stopped
    amendment['worker_template']=(Path(fixed.__file__).parents[1]/'references/workers/pass-slice-compact.md').read_text()
    amendment['input_digest']=mutation_digest(amendment)
    current,begun=begin_amended(adapter,value,amendment,native_worker=True)
    binding=begun['worker_request']
    card=next(c for c in current['kanban']['task_map'] if c['task_id']==binding['task_id'])
    path=vault/f"_system/ledgers/ingest-workflows/{current['workflow_id']}/bindings/{fingerprint(card['idempotency_key'])}.json"
    path.with_suffix('.execution.json').write_text(json.dumps({'state':'execution_blocked','run_id':17}))
    leased=adapter.workflow.knowledge._slice(current['batch_id'],binding['node'].partition(':')[2])
    stamp=datetime.fromisoformat(leased['lease']['claimed_at'].replace('Z','+00:00')).timestamp()
    row=next(r for r in adapter.kanban.by_id.values() if r['id']==binding['task_id'])
    snapshot={'task':{'id':binding['task_id'],'status':'running' if live else 'archived',
        'worker_pid':123 if live else None,'current_run_id':17 if live else None,
        'created_by':'ingest-workflow','body':json.dumps(row['body'])},
        'runs':[{'id':17,'started_at':stamp-1,'ended_at':None if live else stamp+1}]}
    monkeypatch.setattr(adapter.kanban,'task_snapshot',lambda *a:snapshot)
    adapter.execution_failure(binding,'LOCK_TIMEOUT','isolated shared lock failure')
    adapter._settle_ended_pass_leases(current)
    after=adapter.workflow.knowledge._slice(current['batch_id'],leased['slice_id'])
    assert bool(after['lease']['worker_id'])==live
    if not live:
        assert after['state']=='blocked'
        adapter._settle_ended_pass_leases(current)  # Idempotent after settlement.


def test_multimodal_framing_is_exact_and_image_groups_keep_authority():
    from fixed_semantic_media import message_content,normalize_content,select_images,MAX_IMAGE_BYTES
    tasks=[{'task':f't{i}','materials':[{'ref':'m1','role':'core','text_ref':'x','asset':{'path':f'image-{i}'}}],
            'continuation':{'action':'candidate_then_citation'},'next_sequence':0} for i in range(1,4)]
    catalog={f'image-{i}':{'url':f'data:image/png;base64,exact-{i}','bytes':MAX_IMAGE_BYTES//2,
        'sha256':str(i),'media_type':'image/png'} for i in range(1,4)}
    packet={'tasks':tasks,'texts':{'x':''},'headings':[]}
    groups=fixed.pass_groups(packet,tasks,'candidate',30000,catalog)
    assert [len(g) for g in groups]==[2,1]
    images=select_images(groups[0],catalog)
    message=message_content('exact task',images)
    converted=[{'type':'input_text','text':message[0]['text']},
        *[{'type':'input_image','image_url':i['url'],'detail':'high'} for i in images]]
    assert normalize_content(message)==normalize_content(converted)
    converted[1]['image_url']+='changed'
    assert normalize_content(message)!=normalize_content(converted)
    assert normalize_content([{'type':'tool_call','text':'extra context'}]) is None
    with pytest.raises(ContractError,match='UNINSPECTED_SUPPORT'): select_images(tasks,{})


def test_image_transport_verifies_registered_bytes_and_ignores_linked_assets(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from fixed_semantic_media import image_catalog
    vault=tmp_path/'vault';root=vault/'_system/sources/artifacts/resource/revision';root.mkdir(parents=True)
    path=root/'figure.png';path.write_bytes(b'registered image bytes')
    manifest={'artifact_revision':'revision','assets':[{'path':'figure.png','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'media_type':'image/png'}]}
    source=SimpleNamespace(_artifact_path=lambda p:p,_verify_artifact=lambda p:(manifest,'',[]))
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=vault,knowledge=SimpleNamespace(source=source)))
    asset={'path':'_system/sources/artifacts/resource/revision/figure.png','media_type':'image/png'}
    packet={'tasks':[{'task':'t1','materials':[{'ref':'m1','asset':asset},{'ref':'m2','linked_assets':[asset]}]}]}
    catalog=image_catalog(adapter,packet)
    assert list(catalog)==[asset['path']] and catalog[asset['path']]['bytes']==len(path.read_bytes())
    path.write_bytes(b'changed image bytes')
    with pytest.raises(ContractError,match='SOURCE_CHANGED'): image_catalog(adapter,packet)


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
