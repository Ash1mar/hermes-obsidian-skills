"""Prepared single-Pass commits retain live authority on isolated Vaults."""
import json
import os
import shutil

import pytest

from test_compact_execution import stopped, vault, begin_amended, packet_views, semantic_draft
from bound_pass_submission import submit_bound
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units.workflow_guard import _ACTIVE


def started(stopped, vault):
    adapter, value, amendment = stopped
    current, begun = begin_amended(adapter, value, amendment)
    bound = begun['worker_request']
    views = packet_views(vault, begun)
    payload = {'passes': [semantic_draft(views[1], 't2')]}
    return adapter, bound, payload, views


def test_prepared_pass_validates_units_outside_guard_and_preserves_replay(stopped, vault, monkeypatch):
    adapter, bound, payload, _ = started(stopped, vault)
    original = FileKnowledgeBuildService._live_units
    observed = []

    def live_units(self, *args, **kwargs):
        observed.append(_ACTIVE.get())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FileKnowledgeBuildService, '_live_units', live_units)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert result['ok'] and result['results'][0]['created']
    assert observed == [None], 'whole-UnitSet validation must happen once outside commit locks'
    receipt = result['results'][0]
    before = (vault / receipt['path']).read_bytes()
    replay = submit_bound(vault, bound, payload, adapter=adapter)
    assert replay['ok'] and not replay['results'][0]['created']
    assert (vault / receipt['path']).read_bytes() == before
    assert (vault / receipt['preflight_ref']).exists()
    assert adapter.workflow.knowledge._slice(
        adapter.workflow.status(bound['workflow_id'])['batch_id'],
        bound['node'].partition(':')[2])['state'] == 'leased'


@pytest.mark.parametrize('changed', ['package', 'engine', 'registry', 'source', 'task'])
def test_prepared_pass_rejects_change_before_commit(stopped, vault, monkeypatch, changed):
    adapter, bound, payload, _ = started(stopped, vault)
    original = FileKnowledgeBuildService.prepare_pass
    task_id = adapter.worker_check(bound)['task_ids'][1]
    pass_root = vault / f'_system/knowledge-builds/task-{task_id}/passes'
    before = {p: p.read_bytes() for p in pass_root.glob('*.json')}
    mutated = []

    def prepare(self, request, **kwargs):
        result = original(self, request, **kwargs)
        package = result['package']
        ref = package['window']['core_refs'][0]['unit_ref']
        if changed == 'package':
            path = vault / f'_system/knowledge-builds/task-{request["task_id"]}/readings/{package["package_id"]}.json'
        elif changed == 'engine':
            path = vault / f'_system/sources/units/{ref["resource_id"]}/{ref["unit_set_id"]}/engine.json'
        elif changed == 'registry':
            path = vault / '_system/metadata/document-registry.json'
        elif changed == 'source':
            path = vault / f'_system/sources/artifacts/{ref["resource_id"]}/{ref["artifact_revision"]}/document.md'
        else:
            path = self._task_path(request['task_id'])
            task = json.loads(path.read_text())
            task['revision'] += 1
            path.write_text(json.dumps(task))
            mutated.append(path)
            return result
        # Preserve valid JSON where applicable; the byte/signature change is
        # nevertheless incompatible with the checked immutable snapshot.
        path.write_bytes(path.read_bytes() + b'\n ')
        mutated.append(path)
        return result

    monkeypatch.setattr(FileKnowledgeBuildService, 'prepare_pass', prepare)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert mutated and not result['ok']
    assert result['failures'][0]['code'] == ('STALE_INPUT' if changed == 'task' else 'SOURCE_CHANGED')
    assert {p: p.read_bytes() for p in pass_root.glob('*.json')} == before


def test_prepared_pass_keeps_single_receipt_when_next_validation_fails(stopped, vault, monkeypatch):
    adapter, bound, payload, views = started(stopped, vault)
    original = FileKnowledgeBuildService.prepare_pass
    # Reuse the fixture's existing candidate task as the second authorized slot.
    # Its payload can be read from the current registered packet.
    payload['passes'].append(semantic_draft(views[0], 't1'))
    calls = []

    def prepare(self, request, **kwargs):
        calls.append(request['task_id'])
        if len(calls) > 1:
            from hermes_source_units import ContractError
            raise ContractError('SOURCE_CHANGED', '$', 'isolated second-slot evidence change')
        return original(self, request, **kwargs)

    monkeypatch.setattr(FileKnowledgeBuildService, 'prepare_pass', prepare)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert not result['ok'] and len(result['results']) == 1
    assert result['results'][0]['task'] == 't2'
    assert (vault / result['results'][0]['path']).exists()
    assert (vault / result['results'][0]['preflight_ref']).exists()
    assert result['failures'][0]['code'] == 'SOURCE_CHANGED'


def test_prepared_revision_tracks_immutable_predecessor(stopped, vault):
    adapter, bound, payload, views = started(stopped, vault)
    first = submit_bound(vault, bound, payload, adapter=adapter)['results'][0]
    confirmation = {'confirmations': [{
        'task': 't2', 'input_id': views[1]['input_id'],
        'candidate_pass_id': first['pass_id'], 'decision': 'confirmed_unchanged',
        'review_note': 'Isolated fixture rechecked unchanged support.'}]}
    citation = submit_bound(vault, bound, confirmation, confirmation=True, adapter=adapter)['results'][0]
    prior_path = vault / citation['path']
    prior = json.loads(prior_path.read_text())
    service = FileKnowledgeBuildService(vault)
    task = service._task(prior['task_id'])
    draft = {key: prior[key] for key in ('task_id', 'actor', 'reading_package_id',
                                        'inspections', 'candidates', 'empty_reason')}
    draft.update(expected_revision=task['revision'], registry_revision=1,
                 pass_kind='citation', sequence=2)
    revision = {'revision_id': 'pr-isolated-predecessor', 'tasks': [{
        'task_id': task['task_id'], 'sequence': 2,
        'reading_package_id': prior['reading_package_id'],
        'pass_id': prior['pass_id'], 'pass_ref': citation['path']}]}
    before = set(prior_path.parent.glob('*.json'))
    with pytest.raises(ContractError, match='SOURCE_CHANGED'):
        with service.source.reading_snapshot(verify_hashes=True):
            service.prepare_pass(draft, batch_id=prior['batch_id'],
                                 template_hash=prior['template_hash'], pass_revision=revision)
            prior_path.write_bytes(prior_path.read_bytes() + b'\n ')
            service.source.assert_reading_snapshot_current()
    assert set(prior_path.parent.glob('*.json')) == before


@pytest.mark.skipif(os.name != 'posix', reason='Canonical symlink checks run on the deployed Linux runtime')
@pytest.mark.parametrize('initial_alias', [False, True])
def test_prepared_package_rejects_canonical_symlink_target_change(stopped, vault, monkeypatch, initial_alias):
    adapter, bound, payload, _ = started(stopped, vault)
    original = FileKnowledgeBuildService.prepare_pass
    task_id = adapter.worker_check(bound)['task_ids'][1]
    canonical = next((vault / f'_system/knowledge-builds/task-{task_id}/readings').glob('*.json'))
    first = vault / '_system/reports/isolated-reading-first.json'
    second = vault / '_system/reports/isolated-reading-second.json'
    first.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(canonical, first)
    shutil.copy2(canonical, second)
    if initial_alias:
        canonical.unlink()
        canonical.symlink_to(first)
    pass_root = vault / f'_system/knowledge-builds/task-{task_id}/passes'
    before = {p: p.read_bytes() for p in pass_root.glob('*.json')}

    def prepare(self, request, **kwargs):
        result = original(self, request, **kwargs)
        canonical.unlink()
        canonical.symlink_to(second)
        return result

    monkeypatch.setattr(FileKnowledgeBuildService, 'prepare_pass', prepare)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert not result['ok'] and result['failures'][0]['code'] == 'SOURCE_CHANGED'
    assert {p: p.read_bytes() for p in pass_root.glob('*.json')} == before


@pytest.mark.parametrize('changed', ['package', 'engine', 'registry', 'source'])
def test_prepared_pass_rejects_byte_change_with_same_size_and_mtime(stopped, vault, monkeypatch, changed):
    adapter, bound, payload, _ = started(stopped, vault)
    original = FileKnowledgeBuildService.prepare_pass
    task_id = adapter.worker_check(bound)['task_ids'][1]
    pass_root = vault / f'_system/knowledge-builds/task-{task_id}/passes'
    before = {p: p.read_bytes() for p in pass_root.glob('*.json')}

    def prepare(self, request, **kwargs):
        result = original(self, request, **kwargs)
        package = result['package']
        ref = package['window']['core_refs'][0]['unit_ref']
        paths = {
            'package': vault / f'_system/knowledge-builds/task-{task_id}/readings/{package["package_id"]}.json',
            'engine': vault / f'_system/sources/units/{ref["resource_id"]}/{ref["unit_set_id"]}/engine.json',
            'registry': vault / '_system/metadata/document-registry.json',
            'source': vault / f'_system/sources/artifacts/{ref["resource_id"]}/{ref["artifact_revision"]}/document.md',
        }
        path = paths[changed]
        stat = path.stat()
        data = path.read_bytes()
        # A byte change with identical metadata must fail the strong snapshot,
        # including whitespace-only changes in otherwise equivalent JSON.
        if b'\n' in data:
            changed_bytes = data.replace(b'\n', b' ', 1)
        elif b' ' in data:
            changed_bytes = data.replace(b' ', b'\t', 1)
        else:
            changed_bytes = bytes([data[0] ^ 1]) + data[1:]
        path.write_bytes(changed_bytes)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns
        return result

    monkeypatch.setattr(FileKnowledgeBuildService, 'prepare_pass', prepare)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert not result['ok'] and result['failures'][0]['code'] == 'SOURCE_CHANGED'
    assert {p: p.read_bytes() for p in pass_root.glob('*.json')} == before


def test_strong_preparation_rehashes_before_reusing_old_metadata_cache(stopped, vault, monkeypatch):
    adapter, bound, payload, _ = started(stopped, vault)
    original = FileKnowledgeBuildService.prepare_pass
    task_id = adapter.worker_check(bound)['task_ids'][1]
    pass_root = vault / f'_system/knowledge-builds/task-{task_id}/passes'
    before = {p: p.read_bytes() for p in pass_root.glob('*.json')}

    def prepare(self, request, **kwargs):
        assert self.source._artifact_cache, 'identity expansion should already have populated the old cache'
        package_path = vault / f'_system/knowledge-builds/task-{task_id}/readings/{request["reading_package_id"]}.json'
        package = json.loads(package_path.read_text())
        ref = package['window']['core_refs'][0]['unit_ref']
        path = vault / f'_system/sources/artifacts/{ref["resource_id"]}/{ref["artifact_revision"]}/document.md'
        stat, data = path.stat(), path.read_bytes()
        changed = data.replace(b'\n', b' ', 1) if b'\n' in data else bytes([data[0] ^ 1]) + data[1:]
        path.write_bytes(changed)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return original(self, request, **kwargs)

    monkeypatch.setattr(FileKnowledgeBuildService, 'prepare_pass', prepare)
    result = submit_bound(vault, bound, payload, adapter=adapter)
    assert not result['ok'] and result['failures'][0]['code'] == 'SOURCE_CHANGED'
    assert {p: p.read_bytes() for p in pass_root.glob('*.json')} == before
