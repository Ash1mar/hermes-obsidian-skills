"""Explicit per-source admission has no clock and cannot release a pause."""
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-source-units/src'))
sys.path.insert(0, str(ROOT / 'hermes-obsidian-controlled-ingest/lib'))
from hermes_source_units import ContractError
from hermes_source_units import incremental_control as module
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.source_units import _json_bytes, _load_json, _write_atomic
import ingest_kanban as kanban


def setup_control(tmp_path, manual=True):
    sources = []
    for name in ('a', 'b', 'c'):
        path = tmp_path / '10_Raw' / (name + '.pdf')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(('isolated source ' + name).encode())
        sources.append({'path': path.relative_to(tmp_path).as_posix(),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    control = IncrementalControl(tmp_path, 'isolated-manual')
    control.initialize('operator', ['mapping'], **({'manual_sources': sources} if manual else {}))
    _write_atomic(control.root.parent / 'run.json', _json_bytes({
        'run_id': control.run_id, 'actor': 'operator', 'source_manifest': sources}))
    return control, sources


def release(control, source, operation_id='release-a'):
    return {'run_id': control.run_id, 'actor': 'operator', 'operation_id': operation_id,
            'expected_revision': control.status()['revision'], 'action': 'release-source',
            'source_path': source['path'], 'source_sha256': source['sha256']}


def test_default_mode_keeps_sources_automatically_admitted(tmp_path):
    control, sources = setup_control(tmp_path, manual=False)
    assert 'manual_source_admission' not in control.status()
    assert all(control.source_allowed(row['path'], row['sha256']) for row in sources)
    assert control.initialize('operator', ['mapping'])['revision'] == 1


def test_manual_manifest_holds_every_source_until_exact_manual_release(tmp_path):
    control, sources = setup_control(tmp_path)
    assert not any(control.source_allowed(row['path'], row['sha256']) for row in sources)
    request = release(control, sources[1], 'release-b')
    result = control.control(request)
    assert result['state'] == 'active'
    assert [control.source_allowed(row['path'], row['sha256']) for row in sources] == [False, True, False]
    assert control.control(request) == result
    assert control.status()['revision'] == 2
    assert control.initialize('operator', ['mapping'], manual_sources=sources)['manual_source_admission'][sources[1]['path']]['state'] == 'released'
    with pytest.raises(ContractError, match='policy changed'):
        control.initialize('operator', ['mapping'])


@pytest.mark.parametrize('state', ['paused', 'pausing'])
def test_release_source_preserves_pause_boundary_and_released_checkpoint_set(tmp_path, state):
    control, sources = setup_control(tmp_path)
    head = control.status()
    boundary = {'scope': {'scope_kind': 'run', 'scope_id': control.run_id},
                'stage': 'mapping', 'input_fingerprint': 'original-input', 'evidence': {}}
    head.update(state=state, boundary=boundary, released=['previous-checkpoint'])
    _write_atomic(control.path, _json_bytes(head))
    result = control.control(release(control, sources[0]))
    assert result['state'] == state
    assert result['boundary'] == boundary
    assert result['released'] == ['previous-checkpoint']
    assert control.source_allowed(sources[0]['path'], sources[0]['sha256'])


@pytest.mark.parametrize('case', ['wrong-hash', 'outside-source', 'manifest-differs', 'cancelled'])
def test_release_source_rejects_changed_or_unauthorized_identity(tmp_path, case):
    control, sources = setup_control(tmp_path)
    request = release(control, sources[0])
    if case == 'wrong-hash':
        request['source_sha256'] = '0' * 64
    elif case == 'outside-source':
        request['source_path'] = '10_Raw/outside.pdf'
    elif case == 'manifest-differs':
        run = _load_json(control.root.parent / 'run.json')
        run['source_manifest'] = sources[1:]
        _write_atomic(control.root.parent / 'run.json', _json_bytes(run))
    else:
        head = control.status()
        head['state'] = 'cancelled'
        _write_atomic(control.path, _json_bytes(head))
    with pytest.raises(ContractError):
        control.control(request)
    assert not any(control.source_allowed(row['path'], row['sha256']) for row in sources)
    assert not (control.root / 'operations' / (request['operation_id'] + '.json')).exists()


def source_node(source):
    return SimpleNamespace(kind='source-prepare', name='source-prepare:' + module.fingerprint({
        'path': source['path'], 'content_sha256': source['sha256']})[:16], input_fingerprint='source-input')


def test_source_dispatch_admission_filters_before_window_and_cannot_use_revision_bypass(tmp_path, monkeypatch):
    control, sources = setup_control(tmp_path)
    control.control(release(control, sources[2], 'release-c'))
    workflow = {'workflow_id': 'ingest-isolated-manual',
                'scope': {'workflow_mode': 'incremental_v1', 'incremental_run_id': control.run_id,
                          'source_paths': [row['path'] for row in sources]},
                'current_stage': 'source_preparing', 'batch_id': None}
    adapter = kanban.IngestKanbanAdapter.__new__(kanban.IngestKanbanAdapter)
    adapter.workflow = SimpleNamespace(vault=tmp_path, revision_allows=lambda *_: True, pending_pause=lambda _: False)
    adapter.source_prepare_concurrency = 1
    monkeypatch.setattr(adapter, '_failed_card', lambda *_: False)
    monkeypatch.setattr(kanban, 'domain_completed', lambda *_: False)
    nodes = [source_node(source) for source in sources]
    assert adapter._source_window(workflow, nodes) == {nodes[2].name}
    assert not adapter._canary_allows(workflow, nodes[0])
    assert adapter._canary_allows(workflow, nodes[2])
    changed = source_node({**sources[2], 'sha256': '0' * 64})
    assert not adapter._source_admitted(workflow, changed)


def test_worker_begin_cannot_bypass_held_source(tmp_path, monkeypatch):
    control, sources = setup_control(tmp_path)
    node = source_node(sources[0])
    workflow = {'workflow_id': 'ingest-isolated-manual', 'cancel_requested': False, 'batch_id': None,
                'scope': {'workflow_mode': 'incremental_v1', 'incremental_run_id': control.run_id,
                          'source_paths': [row['path'] for row in sources]}}
    adapter = kanban.IngestKanbanAdapter.__new__(kanban.IngestKanbanAdapter)
    adapter.enable_workers = True
    adapter.workflow = SimpleNamespace(vault=tmp_path, status=lambda _: workflow, pinned_templates=lambda _: None)
    monkeypatch.setattr(adapter, 'validate_worker_request', lambda *_a, **_k: None)
    monkeypatch.setattr(kanban, 'desired_graph', lambda *_: [node])
    with pytest.raises(ContractError, match='SOURCE_ADMISSION_HELD'):
        adapter._pre_worker({'workflow_id': workflow['workflow_id'], 'node': node.name}, beginning=True)
