"""Typed submission contracts and explicit citation review, on isolated Vaults."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_compact_execution import stopped, vault, begin_amended, packet_views, semantic_draft, ROOT
from hermes_source_units import ContractError
from hermes_source_units.semantic_submission import validate_submission
from hermes_source_units.validation import fingerprint
from bound_pass_submission import submit_bound


@pytest.mark.parametrize('field,value', [('conditions', 'AND condition'), ('exceptions', None),
    ('support_refs', 'm1'), ('kind', 'invented')])
def test_types_reject_format_errors_before_evidence_access(field, value):
    payload = {'passes': [semantic_draft({'next_sequence': 0, 'input_id': 'a'*64,
        'materials': [{'ref': 'm1', 'role': 'core'}]})]}
    payload['passes'][0]['candidates'][0][field] = value
    with pytest.raises(ContractError, match='INVALID_SCHEMA'):
        validate_submission(payload)


def test_one_call_preflight_commit_explicit_review_and_replay(stopped, vault):
    adapter, value, amendment = stopped
    current, begun = begin_amended(adapter, value, amendment)
    bound = begun['worker_request']
    views = packet_views(vault, begun)
    # The first task already has a valid candidate; another is new. No automatic
    # citation is produced by a candidate submission.
    payload = {'passes': [semantic_draft(views[1], 't2')]}
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert result['ok'] and result['validated'] and len(result['results']) == 1
    receipt = result['results'][0]
    record = json.loads((vault/receipt['path']).read_text())
    audit = json.loads((vault/receipt['preflight_ref']).read_text())
    assert record['sequence'] == 0 and audit['pass_id'] == record['pass_id']
    assert audit['binding']['worker_request_digest'] == fingerprint(bound)
    assert len(list((vault/receipt['path']).parent.glob('*.json'))) == 1
    replay = submit_bound(vault, bound, payload, adapter=adapter)
    assert replay['ok'] and not replay['results'][0]['created']
    confirmation = {'confirmations': [{'task': 't2', 'input_id': views[1]['input_id'],
        'candidate_pass_id': record['pass_id'], 'decision': 'confirmed_unchanged',
        'review_note': 'Rechecked bounded support, logic and original QA; no changes needed.'}]}
    before = {p: p.read_bytes() for p in vault.rglob('*.json')}
    invalid = copy.deepcopy(confirmation)
    invalid['confirmations'][0]['candidate_pass_id'] = '0'*64
    with pytest.raises(ContractError, match='STALE_INPUT'):
        submit_bound(vault, bound, invalid, confirmation=True, adapter=adapter)
    assert before == {p: p.read_bytes() for p in vault.rglob('*.json')}
    confirmed = submit_bound(vault, bound, confirmation, confirmation=True, adapter=adapter)
    assert confirmed['ok']
    citation = json.loads((vault/confirmed['results'][0]['path']).read_text())
    review = json.loads((vault/confirmed['results'][0]['review_ref']).read_text())
    assert citation['sequence'] == 1 and citation['pass_kind'] == 'citation'
    for field in ('candidates', 'inspections', 'empty_reason'):
        assert citation[field] == record[field]
    assert review['citation_pass_id'] == citation['pass_id']
    assert review['review']['candidate_pass_id'] == record['pass_id']
    assert review['review']['review_note'] == confirmation['confirmations'][0]['review_note']
    assert not submit_bound(vault, bound, confirmation, confirmation=True, adapter=adapter)['results'][0]['created']
    # A legacy partial candidate is equally reviewable: no prep/plan replay.
    tid = adapter.worker_check(bound)['task_ids'][0]
    prior = adapter.workflow.knowledge._pass_index([tid])[1][tid][0]
    confirmation['confirmations'][0].update(task='t1', input_id=views[0]['input_id'], candidate_pass_id=prior['pass_id'])
    assert submit_bound(vault, bound, confirmation, confirmation=True, adapter=adapter)['ok']
    # This API cannot release a pause or complete a native worker.
    assert adapter.workflow.knowledge._slice(current['batch_id'], bound['node'].split(':')[1])['state'] == 'leased'
    assert not adapter.workflow.knowledge._batch(current['batch_id']).get('resource_reduction_ids')


def test_lease_rechecked_after_evidence_validation_before_write(stopped, vault, monkeypatch):
    adapter, value, amendment = stopped
    current, begun = begin_amended(adapter, value, amendment)
    from hermes_source_units import FileKnowledgeBuildService
    original_units = FileKnowledgeBuildService._live_units
    original_check = adapter.worker_check
    validated = False
    def live_units(self, *args, **kwargs):
        nonlocal validated
        result = original_units(self, *args, **kwargs)
        validated = True
        return result
    def check(binding):
        if validated:
            raise ContractError('STALE_INPUT', '$', 'lease changed during evidence validation')
        return original_check(binding)
    monkeypatch.setattr(FileKnowledgeBuildService, '_live_units', live_units)
    monkeypatch.setattr(adapter, 'worker_check', check)
    before = set((vault/'_system/knowledge-builds').rglob('*.json'))
    result = submit_bound(vault, begun['worker_request'],
        {'passes': [semantic_draft(packet_views(vault, begun)[1], 't2')]}, adapter=adapter)
    assert not result['ok'] and result['failures'][0]['code'] == 'STALE_INPUT'
    assert set((vault/'_system/knowledge-builds').rglob('*.json')) == before


def test_native_plugin_schema_and_automatic_binding_refresh(stopped, vault, monkeypatch):
    adapter, value, amendment = stopped
    current, begun = begin_amended(adapter, value, amendment)
    workspace = vault/'native-workspace'
    workspace.mkdir()
    binding_path = workspace/'worker-request.json'
    binding_path.write_text(json.dumps(begun['worker_request']))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE', str(workspace))
    monkeypatch.setenv('HERMES_KANBAN_TASK', begun['worker_request']['task_id'])
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1')
    plugin = ROOT/'hermes-obsidian-governed-ingest-orchestrator/native-plugin/ingest-pass-tools/__init__.py'
    spec = importlib.util.spec_from_file_location('typed_native_plugin', plugin)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    class Context:
        tools = {}
        def get_config(self, key, default): return str(ROOT)
        def register_tool(self, **tool): self.tools[tool['name']] = tool
    ctx = Context(); module.register(ctx)
    assert len(ctx.tools) == 8
    assert not ctx.tools['ingest_control_workflow']['check_fn']()
    tool = ctx.tools['ingest_submit_passes']
    item = tool['schema']['parameters']['properties']['passes']['items']
    assert item['properties']['candidates']['items']['properties']['conditions']['type'] == 'array'
    assert 'worker_id' not in item['properties'] and tool['check_fn']()
    old_revision = begun['worker_request']['expected_revision']
    payload = {'vault': str(vault), 'passes': [semantic_draft(packet_views(vault, begun)[1], 't2')]}
    invalid = copy.deepcopy(payload); invalid['passes'][0]['candidates'][0]['conditions'] = 'not an array'
    assert not json.loads(tool['handler'](invalid))['ok']
    assert json.loads(binding_path.read_text())['expected_revision'] == old_revision
    submitted = json.loads(tool['handler'](payload))
    assert submitted['ok'], submitted
    refreshed = json.loads(binding_path.read_text())
    assert refreshed['expected_revision'] > old_revision
    assert adapter.worker_check(refreshed)['ok']
    assert not list(workspace.glob('.semantic-submit-*'))
    # Stale bindings are never silently recovered or rewritten.
    binding_path.write_text(json.dumps(begun['worker_request']))
    stale = binding_path.read_bytes()
    rejected = json.loads(ctx.tools['ingest_pass_heartbeat']['handler']({'vault': str(vault)}))
    assert not rejected['ok'] and rejected['code'] == 'STALE_INPUT'
    assert binding_path.read_bytes() == stale
