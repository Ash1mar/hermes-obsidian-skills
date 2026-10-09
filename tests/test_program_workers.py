"""Isolated program contracts; fixture semantics are never real-workflow acceptance."""
import json

import pytest

from test_p3_knowledge_build import vault, test_auto_full_dispatches_through_both_checkpoints_and_acceptance as _downstream
from test_ingest_worker_recovery import setup_worker
from hermes_source_units import ContractError
from program_worker import canonical_binding, sync_provider


def test_program_downstream_retains_pauses_and_review_contracts(vault, monkeypatch):
    _downstream(vault, monkeypatch, pause_mode=True, provider='skip', program_execution=True)


def test_program_binding_rejects_copy_and_unpinned_executor(tmp_path, monkeypatch):
    root, adapter, value, request = setup_worker(tmp_path)
    row = next(r for r in adapter.kanban.tasks.values() if r['id'] == request['task_id'])
    path = row['body']['worker_binding']
    bound, _ = canonical_binding(adapter, path)
    copied = root / 'copied-binding.json'
    copied.write_text(json.dumps(bound))
    with pytest.raises(ContractError, match='STALE_INPUT'):
        canonical_binding(adapter, copied)
    original = adapter.workflow.pinned_templates
    monkeypatch.setattr(adapter.workflow, 'pinned_templates', lambda w: {k:'legacy model contract' for k in original(w)})
    with pytest.raises(ContractError, match='ACCESS_DENIED'):
        canonical_binding(adapter, path)


def test_disabled_program_sync_never_calls_provider(tmp_path, monkeypatch):
    root, adapter, value, request = setup_worker(tmp_path)
    config = tmp_path / 'disabled.json'
    config.write_text(json.dumps({'enabled':False, 'transport':'command', 'command':['missing-provider']}))
    monkeypatch.setenv('HERMES_RETRIEVAL_PROVIDER_CONFIG', str(config))
    before = {p:p.read_bytes() for p in root.rglob('*.json')}
    with pytest.raises(ContractError, match='PROVIDER_DISABLED'):
        sync_provider(adapter, request, value)
    assert before == {p:p.read_bytes() for p in root.rglob('*.json')}
