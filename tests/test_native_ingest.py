"""Native mechanics preserve scope and supported control on isolated Vaults."""
import json
from pathlib import Path

import pytest
from test_compact_execution import stopped, vault, begin_amended, packet_views, semantic_draft, workflow_request, start_and_pin
from hermes_source_units import ContractError
from hermes_source_units.model_presentation import present_model_packet
from native_ingest import control_workflow, read_pass_input, worker_action, workflow_status
from native_pass_identity import bind_payload, load_context, evidence_packet


def test_presentation_preserves_text_once_and_task_boundaries():
    packet = {'contract':'bounded-model-packet/v1','texts':{'c1':'one\nshared quotation "x"'},
        'headings':[['H']], 'qa':{'q1':['review table']}, 'qa_rule':'actual restriction',
        'tasks':[{'task':'t1','input_id':'a','materials':[{'ref':'m1','text_ref':'c1','role':'core','qa_ref':'q1'}]},
                 {'task':'t2','input_id':'b','materials':[{'ref':'m1','text_ref':'c1','role':'context'}]}]}
    text = present_model_packet(packet)
    assert text.count(packet['texts']['c1'])==1
    tasks = [json.loads(line[5:]) for line in text.splitlines() if line.startswith('TASK ')]
    assert tasks==packet['tasks'] and 'review table' in text and 'actual restriction' in text


def test_native_begin_read_submit_and_compact_completion(stopped,vault,monkeypatch):
    adapter,value,amendment = stopped
    current,begun = begin_amended(adapter,value,amendment)
    workspace = vault/'worker-space'; workspace.mkdir()
    binding = begun['worker_request']
    (workspace/'worker-request.json').write_text(json.dumps(binding))
    (workspace/'pass-input-descriptor.json').write_text(json.dumps(begun['model_input']))
    monkeypatch.setenv('HERMES_KANBAN_TASK',binding['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    presented = read_pass_input(vault)
    assert presented['ok'] and 'TASK ' in presented['content']
    assert '"input_id"' not in presented['content'] and '"candidate_pass_id"' not in presented['content']
    views = packet_views(vault,begun)
    new = [semantic_draft(view,f't{i}') for i,view in enumerate(views,1) if view['next_sequence']==0]
    for draft in new:
        draft.pop('input_id')
    receipt = worker_action(vault,'submit',{'passes':new})
    assert receipt['ok'] and all(r['next_action']=='review_candidate' for r in receipt['results'])
    assert (vault/receipt['receipt_ref']).is_file()
    assert all('task_id' not in r and 'idempotency_key' not in r for r in receipt['results'])
    assert all('pass_id' not in r for r in receipt['results'])
    reread = read_pass_input(vault)
    assert reread['ok'] and '"candidate_pass_id"' not in reread['content']
    confirmations = [{'task':f't{i}',
        'decision':'confirmed_unchanged','review_note':'Reviewed the authorized evidence and original QA.'}
        for i,view in enumerate(views,1)]
    confirmed = worker_action(vault,'confirm',{'confirmations':confirmations})
    assert confirmed['ok'] and all(r['next_action']=='pass_complete' for r in confirmed['results'])
    full_receipt=json.loads((vault/confirmed['receipt_ref']).read_text())
    assert all((vault/r['review_ref']).is_file() for r in full_receipt['results'])
    replay = worker_action(vault,'confirm',{'confirmations':confirmations})
    assert replay['ok'] and not any(r['created'] for r in replay['results'])
    done = worker_action(vault,'complete')
    assert done['domain_complete'] and done['next_action']=='end_worker' and 'result_refs' not in done
    assert (vault/done['receipt_ref']).is_file()
    assert adapter.workflow.knowledge._slice(current['batch_id'],binding['node'].split(':')[1])['state']=='completed'
    assert not adapter.workflow.knowledge._batch(current['batch_id'])['run_ids']


def test_bound_identities_reject_override_and_unobserved_candidate(stopped,vault,monkeypatch):
    adapter,value,amendment=stopped
    current,begun=begin_amended(adapter,value,amendment)
    workspace=vault/'identity-space';workspace.mkdir()
    binding=begun['worker_request']
    (workspace/'worker-request.json').write_text(json.dumps(binding))
    (workspace/'pass-input-descriptor.json').write_text(json.dumps(begun['model_input']))
    monkeypatch.setenv('HERMES_KANBAN_TASK',binding['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    assert read_pass_input(vault)['ok']
    path,context=load_context(vault,binding,adapter.worker_check(binding))
    views=packet_views(vault,begun)
    alias=next(f't{i}' for i,v in enumerate(views,1) if v['next_sequence']==0)
    confirmation={'confirmations':[{'task':alias,'decision':'confirmed_unchanged','review_note':'Reviewed.'}]}
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match='CANDIDATE_NOT_OBSERVED'):
        bind_payload(confirmation,context,confirmation=True)
    candidate=semantic_draft(views[int(alias[1:])-1],alias)
    candidate['input_id']='0'*64
    with pytest.raises(ContractError,match='STALE_INPUT'):
        bind_payload({'passes':[candidate]},context)
    candidate.pop('input_id');candidate['task']='t999999'
    with pytest.raises(ContractError,match='INVALID_SCHEMA'):
        bind_payload({'passes':[candidate]},context)
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    import copy
    packet=json.loads((vault/begun['model_input']['path']).read_text())
    changed=copy.deepcopy(packet);changed['texts'][next(iter(changed['texts']))]='altered evidence'
    assert evidence_packet(packet)!=evidence_packet(changed)


def test_begin_wait_is_durable_and_does_not_create_binding(stopped,vault,monkeypatch):
    from ingest_kanban import IngestKanbanAdapter
    adapter,value,amendment=stopped
    assert adapter.amend_execution(amendment)['applied']
    current=adapter.workflow.status(value['workflow_id'])
    adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor=current['actor'],expected_revision=current['revision']))
    current=adapter.workflow.status(current['workflow_id'])
    card=next(c for c in current['kanban']['task_map'] if adapter.workflow.knowledge._slice(
        current['batch_id'],c['node'].partition(':')[2])['state']=='ready')
    workspace=vault/'wait-space';workspace.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_TASK',card['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    monkeypatch.setattr(IngestKanbanAdapter,'worker_begin',lambda *a,**kw:
        {'ok':True,'leased':False,'reason':'concurrency_limit','slice':None})
    before=adapter.workflow.knowledge._slices(current['batch_id'])
    result=worker_action(vault,'begin')
    assert result['state']=='waiting' and result['reason']=='concurrency_limit'
    assert result['next_action']=='end_worker' and result['retry_same_request'] is False
    assert not list(workspace.iterdir())
    assert adapter.workflow.knowledge._slices(current['batch_id'])==before
    assert (vault/result['report_ref']).is_file()
    node=next(n for n in __import__('ingest_kanban').desired_graph(adapter.workflow,current) if n.name==card['node'])
    assert adapter._execution_blocked(current['workflow_id'],node)


def test_missing_bounded_input_releases_only_own_lease(stopped,vault,monkeypatch):
    adapter,value,amendment=stopped
    adapter.resume(workflow_request(workflow_id=value['workflow_id'],actor=value['actor'],expected_revision=value['revision']))
    current=adapter.workflow.status(value['workflow_id'])
    card=next(c for c in current['kanban']['task_map'] if adapter.workflow.knowledge._slice(
        current['batch_id'],c['node'].partition(':')[2])['state']=='ready')
    workspace=vault/'unbounded-space';workspace.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_TASK',card['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    passes={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').glob('task-*/passes/*.json')}
    result=worker_action(vault,'begin')
    assert result['code']=='NATIVE_INPUT_UNAVAILABLE' and result['state']=='recovery_required'
    assert result['lease_released'] and result['next_action']=='end_worker'
    slice_record=adapter.workflow.knowledge._slice(current['batch_id'],card['node'].partition(':')[2])
    assert slice_record['state']=='blocked' and not slice_record['lease']['worker_id']
    assert all(p.read_bytes()==data for p,data in passes.items())


def test_worker_cannot_call_operator_controls(stopped,vault,monkeypatch):
    adapter,value,amendment = stopped
    monkeypatch.setenv('HERMES_KANBAN_TASK','other-card')
    before = adapter.workflow.status(value['workflow_id'])
    with pytest.raises(ContractError,match='ACCESS_DENIED'):
        control_workflow(vault,value['workflow_id'],'resume','unauthorized')
    assert adapter.workflow.status(value['workflow_id'])==before


def test_native_begin_resolves_only_own_binding_and_saves_input(stopped,vault,monkeypatch):
    adapter,value,amendment = stopped
    assert adapter.amend_execution(amendment)['applied']
    current = adapter.workflow.status(value['workflow_id'])
    adapter.resume(workflow_request(workflow_id=current['workflow_id'],actor=current['actor'],expected_revision=current['revision']))
    current = adapter.workflow.status(current['workflow_id'])
    card = next(c for c in current['kanban']['task_map'] if adapter.workflow.knowledge._slice(
        current['batch_id'],c['node'].split(':')[1])['state']=='ready')
    workspace = vault/'begin-space'; workspace.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_TASK',card['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace))
    result = read_pass_input(vault,begin=True)
    assert result['ok'] and result['task_count']>0 and 'BEGIN TEXT' in result['content']
    binding = json.loads((workspace/'worker-request.json').read_text())
    assert binding['task_id']==card['task_id'] and adapter.worker_check(binding)['ok']
    (workspace/'pass-input-descriptor.json').write_text(json.dumps({'path':'_system/vault.json'}))
    with pytest.raises(ContractError,match='STALE_INPUT'):
        read_pass_input(vault)


def test_control_builds_current_request_and_replays_without_dispatch(stopped,vault,monkeypatch):
    adapter,value,amendment = stopped
    import orchestration
    calls = []
    def dispatch(vault_arg,action,request):
        calls.append((action,request))
        assert request['actor']==value['actor'] and request['expected_revision']==value['revision']
        assert request['input_digest']
        return {'state':'cancelled','background_dispatch':False,'workflow_id':value['workflow_id']}
    monkeypatch.setattr(orchestration,'dispatch',dispatch)
    result = control_workflow(vault,value['workflow_id'],'cancel','one-operation')
    assert result['ok'] and workflow_status(vault,value['workflow_id'])['pass_records']>0
    assert control_workflow(vault,value['workflow_id'],'cancel','one-operation')['replayed']
    assert len(calls)==1
    with pytest.raises(ContractError,match='IDEMPOTENCY_CONFLICT'):
        control_workflow(vault,value['workflow_id'],'resume','one-operation')


def test_ambiguous_discovery_lists_scope_without_mutation(stopped,vault):
    adapter,value,amendment = stopped
    start_and_pin(vault,workflow_request(workflow_id='ingest-other-workflow',actor='agent',expected_revision=0,
        profile='compact-3',batch_id='compact-batch',scope={'source_paths':[],
        'knowledge_selector':'all-current','execution_mode':'auto_full','pause_after':['pass']}))
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    observed=workflow_status(vault)
    assert observed['code']=='AMBIGUOUS_WORKFLOW'
    assert {c['workflow_id'] for c in observed['candidates']}=={value['workflow_id'],'ingest-other-workflow'}
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    current=workflow_status(vault,value['workflow_id'])
    assert current['ok'] and current['current_pass_revision']==value.get('pass_revision')
    assert current['execution_config'] is None
    assert current['execution_journal_pending'] is False
    assert adapter.amend_execution(amendment)['applied']
    current=workflow_status(vault,value['workflow_id'])
    assert current['execution_config']['slice_max_tasks']==amendment['max_tasks']
