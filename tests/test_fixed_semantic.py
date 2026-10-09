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


def fixture_response(value):
    raw=json.dumps(value)
    return raw,{'origin':'isolated_fixture','response_sha256':fingerprint(raw)}


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
    def caller(phase,view,output_schema,correction):
        calls.append(phase)
        assert '"input_id"' not in json.dumps(view) and '"candidate_pass_id"' not in json.dumps(view)
        assert correction is None
        if phase=='candidate':
            drafts=[]
            for task in view['tasks']:
                draft=semantic_draft({**task,'input_id':'fixture-only'},task['task'])
                draft.pop('input_id'); draft.pop('sequence'); drafts.append(draft)
            return fixture_response({'drafts':drafts})
        assert phase=='citation'
        return fixture_response({'reviews':[{'task':t['task'],'decision':'confirmed_unchanged',
            'review_note':'Isolated fixture reviewed source conditions.'} for t in view['tasks']]})
    result=fixed.run_pass(adapter,binding,caller)
    assert result['domain_complete'] and calls==['candidate','citation']
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
        'knowledge_selector':'all-current','execution_mode':'auto_full','provider':'skip','pause_after':['checkpoint_1']}))
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
