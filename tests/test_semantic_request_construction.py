"""Pure request construction and fake SDK boundaries; no native/model execution."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-source-units/src'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-controlled-ingest/lib'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/lib'))

import fixed_semantic as fixed
from fixed_semantic_media import message_content, normalize_content
from fixed_semantic_schema import slot_schema
from hermes_source_units import ContractError, runtime_telemetry


@contextmanager
def no_span(*args, **kwargs):
    yield {'span_id': 'isolated-construction-span'}


def sample_view():
    return {'texts': {'c2': 'Condition OR exception.', 'c1': 'Required AND condition.'},
        'headings': {'0': ['Scope']}, 'qa': {'q1': {'quality': 'usable'}},
        'qa_rule': 'Retain uncertainty.', 'tasks': [
            {'task': 't1', 'materials': [
                {'ref': 'm2', 'role': 'context', 'text_ref': 'c2', 'heading': '0'},
                {'ref': 'm1', 'role': 'core', 'text_ref': 'c1', 'heading': '0',
                 'qa_ref': 'q1', 'required_dependency': True}],
             'candidate': {'candidates': [{'candidate_id': 'one', 'conditions': ['AND'],
                'exceptions': ['OR'], 'finding': 'Bounded finding.', 'support_refs': ['m1']}]},
             'limits': {'context_truncated': True, 'omitted_material_count': 2}},
            {'task': 't2', 'materials': []}]}


def reversed_objects(value):
    if isinstance(value, dict):
        return {key: reversed_objects(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reversed_objects(item) for item in value]
    return value


def test_stable_wire_ignores_object_insertion_and_native_card_metadata_preserves_arrays(monkeypatch):
    view = sample_view()
    output = slot_schema('citation', view['tasks'])
    before = copy.deepcopy(view)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'native-card-one')
    first = fixed.wire_prompt('citation', view, output)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'native-card-two')
    second = fixed.wire_prompt('citation', reversed_objects(view), reversed_objects(output))
    assert first == second
    assert first['system'] == fixed.COMMON + '\n' + fixed.RULES['citation']
    assert first['user'].startswith('Semantic input v1\n')
    body = json.loads(first['user'].partition('\n')[2])
    assert body['input'] == before
    assert [task['task'] for task in body['input']['tasks']] == ['t1', 't2']
    assert [material['ref'] for material in body['input']['tasks'][0]['materials']] == ['m2', 'm1']
    assert list(body['input'])[-1] == 'tasks'
    assert view == before
    assert 'native-card-one' not in first['user'] and 'native-card-two' not in first['user']


@pytest.mark.parametrize('changed', ['phase', 'text', 'qa', 'limits', 'material-role', 'required-dependency',
                                   'prior-condition', 'prior-exception', 'task-order', 'material-order', 'correction'])
def test_changed_semantic_input_changes_exact_wire(changed):
    view = sample_view()
    phase = 'citation'
    output = slot_schema(phase, view['tasks'])
    first = fixed.wire_prompt(phase, view, output)
    modified = copy.deepcopy(view)
    correction = None
    if changed == 'phase':
        phase = 'candidate'
    elif changed == 'text':
        modified['texts']['c1'] += ' Changed.'
    elif changed == 'qa':
        modified['qa']['q1']['quality'] = 'uncertain'
    elif changed == 'limits':
        modified['tasks'][0]['limits']['omitted_material_count'] = 3
    elif changed == 'material-role':
        modified['tasks'][0]['materials'][0]['role'] = 'core'
    elif changed == 'required-dependency':
        modified['tasks'][0]['materials'][1]['required_dependency'] = False
    elif changed == 'prior-condition':
        modified['tasks'][0]['candidate']['candidates'][0]['conditions'] = ['OR']
    elif changed == 'prior-exception':
        modified['tasks'][0]['candidate']['candidates'][0]['exceptions'] = []
    elif changed == 'task-order':
        modified['tasks'].reverse()
    elif changed == 'material-order':
        modified['tasks'][0]['materials'].reverse()
    else:
        correction = {'code': 'INCOMPLETE_COVERAGE', 'message': 'Inspect m2.'}
    assert fixed.wire_prompt(phase, modified, slot_schema(phase, modified['tasks']), correction) != first


@pytest.fixture
def sdk(monkeypatch):
    """Use real fixed-agent format and request guards around a fake SDK base."""
    agents = []
    databases = []
    requests = []

    class FakeDatabase:
        def __init__(self):
            self.closed = False
            databases.append(self)

        def close(self):
            self.closed = True

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs['model']
            self.provider = kwargs.get('provider', 'isolated-sdk')
            self.session_id = kwargs['session_id']
            self.tools = []
            self.valid_tool_names = set()
            self.closed = False
            agents.append(self)

        def close(self):
            self.closed = True

        def _interruptible_api_call(self, kwargs):
            requests.append(copy.deepcopy(kwargs))
            return kwargs

        def _interruptible_streaming_api_call(self, kwargs, *args, **kw):
            requests.append(copy.deepcopy(kwargs))
            return kwargs

        def run_conversation(self, content, *, system_message):
            self._build_system_prompt(system_message)
            self._interruptible_api_call({'messages': [
                {'role': 'system', 'content': system_message}, {'role': 'user', 'content': content}]})
            return {'final_response': '{"decision":"approved","review_note":"Synthetic fixture."}',
                    'api_calls': 1}

    run_agent = ModuleType('run_agent')
    run_agent.AIAgent = FakeAgent
    state = ModuleType('hermes_state')
    state.SessionDB = FakeDatabase
    agent_package = ModuleType('agent')
    agent_package.__path__ = []
    delegation = ModuleType('agent.delegation_context')
    delegation.delegated_child_context = no_span
    monkeypatch.setitem(sys.modules, 'run_agent', run_agent)
    monkeypatch.setitem(sys.modules, 'hermes_state', state)
    monkeypatch.setitem(sys.modules, 'agent', agent_package)
    monkeypatch.setitem(sys.modules, 'agent.delegation_context', delegation)
    monkeypatch.setattr(fixed, 'span', no_span)
    monkeypatch.setattr(runtime_telemetry, 'span', no_span)
    caller = object.__new__(fixed.HermesCaller)
    caller.model = 'isolated-model'
    caller.runtime = {'provider': 'isolated-provider'}
    caller.input_limit = 100000
    caller.images = []
    return SimpleNamespace(caller=caller, agents=agents, databases=databases, requests=requests)


def guarded_agent(sdk, *, images=None):
    images = images or []
    view = {'tasks': [{'task': 't1'}], 'texts': {'c1': 'Exact bounded material.'}}
    output = slot_schema('citation', view['tasks'])
    prepared = fixed.prepare_request_projection('citation', view, output, images)
    prompt = prepared['prompt']
    content = prepared['content']
    agent = sdk.caller.make_agent()
    agent.fixed_request = {**prompt, 'user': content}
    agent.fixed_text_request = {**prompt, 'user': normalize_content(content)[0]}
    agent.fixed_images = images
    agent.fixed_output_schema = output
    agent.fixed_expected_request = prepared['expected']
    agent.fixed_transport_schema = copy.deepcopy(prepared['transport_schema'])
    agent.fixed_schema_size = prepared['schema_size']
    agent.fixed_input_size = prepared['input_size']
    agent.fixed_construction_counts = copy.deepcopy(prepared['construction_counts'])
    return agent


def api_request(agent, *, form='chat'):
    system = agent.fixed_request['system']
    user = {'role': 'user', 'content': copy.deepcopy(agent.fixed_request['user'])}
    if form == 'responses':
        return {'instructions': system, 'input': [user]}
    if form == 'separate-system':
        return {'system': system, 'messages': [user]}
    return {'messages': [{'role': 'system', 'content': system}, user]}


@pytest.mark.parametrize('form', ['chat', 'responses', 'separate-system'])
def test_actual_sdk_guard_accepts_only_exact_supported_framing(sdk, form):
    agent = guarded_agent(sdk)
    agent._interruptible_api_call(api_request(agent, form=form))
    assert agent.fixed_request_checks == 1
    assert len(sdk.requests) == 1


@pytest.mark.parametrize('changed', ['tools', 'text', 'system', 'extra-message'])
def test_sdk_guard_rejects_extra_tools_context_and_changed_request(sdk, changed):
    agent = guarded_agent(sdk)
    request = api_request(agent)
    if changed == 'tools':
        request['tools'] = [{'type': 'function', 'name': 'unrequested'}]
    elif changed == 'text':
        request['messages'][1]['content'] += ' extra context'
    elif changed == 'system':
        request['messages'][0]['content'] += ' changed instruction'
    else:
        request['messages'].append({'role': 'user', 'content': 'extra context'})
    with pytest.raises(RuntimeError, match='extra tools, context or messages'):
        agent._interruptible_api_call(request)
    assert not sdk.requests and agent.fixed_request_checks == 0


def test_sdk_guard_rejects_changed_image_bytes(sdk):
    images = [{'url': 'data:image/png;base64,AA==', 'bytes': 1,
               'references': [{'task': 't1', 'material': 'm1'}]}]
    agent = guarded_agent(sdk, images=images)
    request = api_request(agent)
    request['messages'][1]['content'][1]['image_url']['url'] = 'data:image/png;base64,AQ=='
    with pytest.raises(RuntimeError, match='extra tools, context or messages'):
        agent._interruptible_api_call(request)
    assert not sdk.requests


def test_sdk_guard_enforces_actual_text_and_schema_budget(sdk):
    agent = guarded_agent(sdk)
    # Text alone fits; the actually injected closed output schema does not.
    agent.fixed_limit = len(json.dumps(agent.fixed_text_request, ensure_ascii=False, separators=(',', ':')))
    assert agent.fixed_schema_size > 0 and agent.fixed_input_size > agent.fixed_limit
    with pytest.raises(RuntimeError, match='actual fixed semantic request exceeds its input bound'):
        agent._interruptible_api_call(api_request(agent))
    assert not sdk.requests


def test_sdk_guard_enforces_whole_image_budget(sdk, monkeypatch):
    agent = guarded_agent(sdk, images=[{'url': 'data:image/png;base64,AA==', 'bytes': 1,
        'references': [{'task': 't1', 'material': 'm1'}]}])
    monkeypatch.setattr(fixed, 'images_fit', lambda images: False)
    with pytest.raises(RuntimeError, match='actual fixed semantic image payload exceeds its bound'):
        agent._interruptible_api_call(api_request(agent))
    assert not sdk.requests


@pytest.mark.parametrize('form', ['chat', 'responses'])
def test_sdk_schema_injection_is_independent_from_canonical_and_next_request(sdk, form):
    agent = guarded_agent(sdk)
    original = copy.deepcopy(agent.fixed_output_schema)
    first = api_request(agent, form=form)
    agent._apply_fixed_format(first)
    injected = first['text']['format']['schema'] if form == 'responses' else first['response_format']['json_schema']['schema']
    injected['properties']['results']['properties']['foreign-slot'] = {'type': 'string'}
    second = api_request(agent, form=form)
    agent._apply_fixed_format(second)
    actual = second['text']['format']['schema'] if form == 'responses' else second['response_format']['json_schema']['schema']
    assert 'foreign-slot' not in actual['properties']['results']['properties']
    assert agent.fixed_output_schema == original


@pytest.mark.parametrize('form', ['chat', 'responses'])
def test_sdk_cannot_use_cached_budget_for_a_changed_actual_schema(sdk, form):
    agent = guarded_agent(sdk)
    request = api_request(agent, form=form)
    agent._apply_fixed_format(request)
    schema = request['text']['format']['schema'] if form == 'responses' else request['response_format']['json_schema']['schema']
    schema['properties']['results']['properties']['foreign-slot'] = {'type': 'string'}
    with pytest.raises(RuntimeError, match='fixed semantic output schema changed'):
        agent._check_fixed_request(request)
    assert not sdk.requests and agent.fixed_request_checks == 0


def test_expected_request_is_immutable_even_if_sdk_staging_object_changes(sdk):
    agent = guarded_agent(sdk)
    agent.fixed_request['user'] += ' SDK staging mutation'
    with pytest.raises(RuntimeError, match='extra tools, context or messages'):
        agent._interruptible_api_call(api_request(agent))
    assert not sdk.requests


@pytest.mark.parametrize('changed', ['phase', 'view', 'prior', 'canonical-schema', 'correction', 'images'])
def test_prepared_request_cannot_be_reused_for_different_complete_input(sdk, changed):
    phase = 'citation'
    view = sample_view()
    output = slot_schema(phase, view['tasks'])
    prepared = fixed.prepare_request_projection(phase, view, output)
    correction = None
    if changed == 'phase':
        phase = 'candidate'
    elif changed == 'view':
        view['texts']['c1'] += ' Changed.'
    elif changed == 'prior':
        view['tasks'][0]['candidate']['candidates'][0]['exceptions'] = []
    elif changed == 'canonical-schema':
        # strict transport deliberately omits minLength. Identity still binds
        # the complete local schema, so this must not reuse its old snapshot.
        output['$defs']['result']['anyOf'][0]['properties']['review_note']['minLength'] = 2
    elif changed == 'correction':
        correction = {'code': 'INVALID_SCHEMA', 'message': 'Different request.'}
    else:
        sdk.caller.images = [{'url': 'data:image/png;base64,AQ==', 'bytes': 1,
            'references': [{'task': 't1', 'material': 'm1'}]}]
    with pytest.raises(RuntimeError, match='prepared semantic request no longer matches this call'):
        sdk.caller._call(phase, view, output, correction, prepared=prepared)
    assert not sdk.agents and not sdk.requests


def test_prepared_projection_does_not_alias_mutable_caller_input():
    view = sample_view()
    output = slot_schema('citation', view['tasks'])
    prepared = fixed.prepare_request_projection('citation', view, output)
    identity = copy.deepcopy(prepared['identity'])
    wire = copy.deepcopy(prepared['wire'])
    view['texts']['c1'] += ' Changed after construction.'
    output['properties']['results']['properties']['foreign'] = {'type': 'string'}
    assert prepared['identity'] == identity and prepared['wire'] == wire


def test_request_scope_rejects_reentry_and_cleans_on_failure(sdk):
    view = sample_view()
    prepared = fixed.prepare_request_projection('citation', view, slot_schema('citation', view['tasks']))
    with pytest.raises(ValueError, match='isolated scope failure'):
        with sdk.caller.request_scope(prepared):
            assert sdk.caller._prepared_request is prepared
            with pytest.raises(RuntimeError):
                with sdk.caller.request_scope(prepared):
                    pytest.fail('nested prepared scopes must not be entered')
            raise ValueError('isolated scope failure')
    assert not hasattr(sdk.caller, '_prepared_request')


def test_closed_projection_cannot_enter_a_second_scope_even_without_sdk_execution(sdk):
    view = sample_view()
    output = slot_schema('citation', view['tasks'])
    prepared = fixed.prepare_request_projection('citation', view, output)
    with sdk.caller.request_scope(prepared):
        pass
    with pytest.raises(RuntimeError, match='prepared semantic request has already been consumed'):
        with sdk.caller.request_scope(prepared):
            pytest.fail('an ended construction scope must not be reused')
    assert not sdk.requests and not hasattr(sdk.caller, '_prepared_request')


def test_consumed_projection_cannot_make_a_second_sdk_call_with_identical_input(sdk):
    view = sample_view()
    output = slot_schema('citation', view['tasks'])
    prepared = fixed.prepare_request_projection('citation', view, output)
    sdk.caller._call('citation', view, output, prepared=prepared)
    with pytest.raises(RuntimeError, match='prepared semantic request has already been consumed'):
        sdk.caller._call('citation', view, output, prepared=prepared)
    assert len(sdk.requests) == 1 and len(sdk.agents) == 1
    assert sdk.agents[0].closed and sdk.databases[0].closed


def test_two_model_calls_use_separate_scopes_and_live_permission_checks(sdk, tmp_path):
    checks = []
    blocked = False
    binding = {'workflow_id': 'isolated-request-construction', 'task_id': 'synthetic-native',
               'node': 'build-finalize', 'template_hash': 'synthetic-template'}

    def live_check(request, **kwargs):
        checks.append(copy.deepcopy(request))
        if blocked:
            raise ContractError('WORKFLOW_STOPPED', '$', 'isolated cancellation')
        return {'ok': True}

    adapter = SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path), worker_check=live_check)
    first = {'page': {'content': 'Bounded synthetic page one.'}}
    second = {'page': {'content': 'Bounded synthetic page two.'}}
    fixed.model_call(adapter, binding, 'page-review', first, sdk.caller, 100000)
    assert not hasattr(sdk.caller, '_prepared_request')
    fixed.model_call(adapter, binding, 'page-review', second, sdk.caller, 100000)
    assert not hasattr(sdk.caller, '_prepared_request')
    assert len(checks) == 2 and len(sdk.requests) == 2
    assert sdk.requests[0]['messages'][1]['content'] != sdk.requests[1]['messages'][1]['content']
    assert all(agent.closed for agent in sdk.agents)
    assert all(database.closed for database in sdk.databases)
    blocked = True
    with pytest.raises(ContractError, match='WORKFLOW_STOPPED'):
        fixed.model_call(adapter, binding, 'page-review', first, sdk.caller, 100000)
    assert len(checks) == 3 and len(sdk.requests) == 2
    assert not hasattr(sdk.caller, '_prepared_request')


def test_future_planning_identity_invalidates_changed_wire_contract_without_measuring(monkeypatch):
    import exact_model_budget as budget
    from hermes_source_units.validation import fingerprint
    rules_before = fingerprint({'common': budget.COMMON, 'rules': budget.RULES,
        'schema': {phase: budget.strict_schema(budget.slot_schema(phase, [{'task': 't1'}]))
                   for phase in ('candidate', 'citation')}})
    before = budget.model_measurement(None, 'isolated-actor', 1, 30000).identity
    monkeypatch.setattr(budget, 'WIRE_CONTRACT', budget.WIRE_CONTRACT + '-changed')
    after = budget.model_measurement(None, 'isolated-actor', 1, 30000).identity
    assert before['request_contract_sha256'] != after['request_contract_sha256']
    assert {key: value for key, value in before.items() if key != 'request_contract_sha256'} == {
        key: value for key, value in after.items() if key != 'request_contract_sha256'}
    assert rules_before == fingerprint({'common': budget.COMMON, 'rules': budget.RULES,
        'schema': {phase: budget.strict_schema(budget.slot_schema(phase, [{'task': 't1'}]))
                   for phase in ('candidate', 'citation')}})


@pytest.mark.parametrize('form', ['chat','responses','separate-system'])
def test_non_slot_outputs_require_native_json_mode_without_changing_canonical_contract(sdk,form):
    agent=guarded_agent(sdk)
    agent.fixed_transport_schema=None
    before=copy.deepcopy(agent.fixed_expected_request)
    agent._interruptible_api_call(api_request(agent,form=form))
    sent=sdk.requests[-1]
    assert (sent['text']['format'] if form=='responses' else sent['response_format'])=={'type':'json_object'}
    assert agent.fixed_expected_request==before
    bad=api_request(agent,form=form)
    if form=='responses':bad['text']={'format':{'type':'text'}}
    else:bad['response_format']={'type':'text'}
    with pytest.raises(RuntimeError,match='JSON output mode changed'):agent._check_fixed_request(bad)


def test_page_caller_forks_have_independent_request_images_and_db_owners(sdk):
    sdk.caller.images=[{'fixture':'parent-only'}]
    sdk.caller._prepared_request={'fixture':'parent-scope'}
    one=sdk.caller.fork();two=sdk.caller.fork()
    assert one is not two and one.images==two.images==[]
    assert one.images is not two.images and not hasattr(one,'_prepared_request')
    assert one.runtime is sdk.caller.runtime and one.model==two.model==sdk.caller.model
    one.images.append({'fixture':'one-only'})
    assert two.images==[] and sdk.caller.images==[{'fixture':'parent-only'}]
