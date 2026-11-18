"""Isolated incremental delivery crash/retry and mixed-version read rejection."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_p4_vault_finalize import completed_page, publish_sources, run_pass, vault
from hermes_source_units import ContractError, FileKnowledgeBuildService, FileVaultFinalizeService
from hermes_source_units.incremental_delivery import (IncrementalDeliveryService, readiness,
                                                       require_index_admission, require_query_available)
from hermes_source_units.source_units import _load_json, _sha, _vault_path
import hermes_source_units.knowledge_build as build_module


def draft_delivery(vault, monkeypatch, *, update=False):
    ref, = publish_sources(vault, ['# Pump\n\nRequires a filtered inlet.\n'])
    parent = completed_page(vault, [ref]) if update else None
    build = FileKnowledgeBuildService(vault)
    task, citation = run_pass(build, 'task-delivery', ref)
    run = build.reduce({'run_id': 'round-build', 'actor': 'agent',
                       'tasks': [{'task_id': task['task_id'], 'revision': task['revision']}],
                       'expected_registry_revision': build._registry()['revision'], 'document_registry_revision': 1,
                       'decisions': [{'candidate_refs': [{'pass_id': citation['pass_id'], 'candidate_id': 'pump'}],
                                      'identity': {'kind': 'entity', 'identity_key': 'project-a:pump-x',
                                                   'canonical_name': 'Pump X', 'aliases': ['PX']},
                                      'action': 'update' if update else 'create', 'path': '30_Cards/pump-x.md',
                                      'content': '# Pump X\n\nRetain the filter and inspect its inlet.\n'}],
                       'reason': 'Incremental fixture'})['run']
    page, = run['page_revisions']
    authorization = {'contract': 'incremental-build-authorization/v1', 'run_id': 'run', 'round_id': 'round',
                     'actor': 'agent', 'pages': [page['page_id']]}
    # A production caller has the independently verified native short Build proof.
    monkeypatch.setattr('hermes_source_units.short_build.evidence',
                        lambda *_: {'scope': {'authorization': authorization}})
    final = {'run_id': run['run_id'], 'actor': 'agent', 'expected_revision': run['revision'],
             'reviews': [{'page_id': page['page_id'], 'authored_sha256': page['authored_sha256'],
                          'parent_authored_sha256': parent['authored_sha256'] if parent else None,
                          'actor': 'reviewer', 'note': 'Fixture verified exact source evidence'}]}
    request = {'operation_id': 'delivery-operation', 'actor': 'agent', 'run_id': 'run', 'round_id': 'round',
               'authorization': authorization, 'build_run_id': run['run_id'], 'finalize_request': final,
               'base_release': None, 'base_release_sha256': None}
    admissions = []
    def admission(scope):
        admissions.append(scope)
        return True
    def native(binding):
        if binding != {'task_id': 'native'}:
            raise ContractError('ACCESS_DENIED', '$', 'wrong native binding')
    service = IncrementalDeliveryService(vault, admission=admission, native_check=native)
    return service, request, page, admissions


def test_build_and_index_interruption_reuse_exact_inputs(vault, monkeypatch):
    service, request, page, admissions = draft_delivery(vault, monkeypatch)
    binding = {'task_id': 'native'}
    prepared = service.prepare(request, binding)
    assert prepared['state'] == 'prepared' and readiness(vault)['available']
    assert not (vault / page['path']).exists()
    assert service.prepare(request, binding) == prepared
    with pytest.raises(ContractError, match='different delivery'):
        service.prepare({**request, 'operation_id': 'different'}, binding)
    calls = []
    def interrupted_index(release_id, release_hash):
        calls.append((release_id, release_hash))
        require_index_admission(vault, release_id, release_hash)
        with pytest.raises(ContractError, match='exact pending'):
            require_index_admission(vault, 'other-release', release_hash)
        with pytest.raises(ContractError, match='incremental-delivery'):
            require_query_available(vault)
        raise RuntimeError('isolated Provider failure')
    with pytest.raises(RuntimeError, match='Provider failure'):
        service.deliver('run', 'round', binding, synchronize=interrupted_index)
    pending = service.status('run', 'round')
    assert pending['state'] == 'index_pending' and len(calls) == 1
    build_bytes = service.build._run_path('round-build').read_bytes()
    page_bytes = (vault / page['path']).read_bytes()
    release_bytes = service.finalize._manifest_path(pending['release_id']).read_bytes()
    def completed_index(release_id, release_hash):
        assert (release_id, release_hash) == calls[0]
        return {'status': 'ready', 'release_id': release_id, 'release_hash': release_hash,
                'index_generation': 'fixed-generation'}
    delivered = service.recover('run', 'round', binding, disposition='continue', synchronize=completed_index,
                                build=lambda *_: pytest.fail('completed Build was called again'))
    assert delivered['state'] == 'committed' and readiness(vault)['available']
    assert service.build._run_path('round-build').read_bytes() == build_bytes
    assert (vault / page['path']).read_bytes() == page_bytes
    assert service.finalize._manifest_path(pending['release_id']).read_bytes() == release_bytes
    assert service.prepare(request, binding) == delivered


def test_uncommitted_window_can_be_aborted_without_domain_writes(vault, monkeypatch):
    service, request, page, _ = draft_delivery(vault, monkeypatch)
    binding = {'task_id': 'native'}
    service.prepare(request, binding)
    def stop_before_write(_):
        raise RuntimeError('no domain writes')
    with pytest.raises(RuntimeError):
        service.deliver('run', 'round', binding, synchronize=None, build=stop_before_write)
    assert readiness(vault)['available'] is False
    aborted = service.recover('run', 'round', binding, disposition='abort_uncommitted')
    assert aborted['state'] == 'aborted' and readiness(vault)['available']
    assert not (vault / page['path']).exists()


def test_native_delivery_stops_before_release_and_acceptance(vault, monkeypatch):
    service, request, page, _ = draft_delivery(vault, monkeypatch)
    binding = {'task_id': 'native'}
    service.prepare(request, binding)
    built = service.deliver('run', 'round', binding, synchronize=lambda *_: pytest.fail('index started early'),
                            stop_after='build_committed')
    assert built['state'] == 'build_committed' and service.finalize.status()['current_release_id'] is None
    build_bytes = service.build._run_path('round-build').read_bytes()
    calls = []
    def index(rid, digest):
        calls.append(rid)
        return {'status': 'ready', 'release_id': rid, 'release_hash': digest, 'index_generation': 'fixed'}
    indexed = service.recover('run', 'round', binding, disposition='continue', synchronize=index,
                              stop_after='index_verified', build=lambda *_: pytest.fail('Build repeated'))
    assert indexed['state'] == 'index_verified' and readiness(vault)['available'] is False
    accepted = service.recover('run', 'round', binding, disposition='continue',
                               synchronize=lambda *_: pytest.fail('index repeated'),
                               build=lambda *_: pytest.fail('Build repeated'))
    assert accepted['state'] == 'committed' and readiness(vault)['available']
    assert len(calls) == 1 and service.build._run_path('round-build').read_bytes() == build_bytes


def test_parent_recovery_only_accepts_the_pinned_partial_commit(vault, monkeypatch):
    service, request, page, _ = draft_delivery(vault, monkeypatch, update=True)
    binding = {'task_id': 'native'}
    service.prepare(request, binding)
    original_write = build_module._write_atomic
    failed = []
    def fail_after_canonical(path, data):
        if str(path).endswith('knowledge-identities.json') and not failed:
            failed.append(True)
            raise RuntimeError('interrupted after canonical commit')
        return original_write(path, data)
    monkeypatch.setattr(build_module, '_write_atomic', fail_after_canonical)
    with pytest.raises(RuntimeError, match='canonical commit'):
        service.deliver('run', 'round', binding, synchronize=None)
    assert failed and _sha((vault / page['path']).read_bytes()) == page['authored_sha256']
    with pytest.raises(ContractError, match='domain writes'):
        service.recover('run', 'round', binding, disposition='abort_uncommitted')
    monkeypatch.setattr(build_module, '_write_atomic', original_write)
    result = service.recover('run', 'round', binding, disposition='continue', synchronize=lambda rid, digest: {
        'status': 'ready', 'release_id': rid, 'release_hash': digest, 'index_generation': 'fixed'})
    assert result['state'] == 'committed'
    assert service.build._registry()['subjects'][0]['current_revision_id'] == page['revision_id']


def test_blocked_window_prevents_provider_reads_before_model_initialization(tmp_path, monkeypatch):
    import qmd_like_rag.runtime as runtime
    from hermes_source_units.source_units import _json_bytes, _write_atomic
    vault = tmp_path / 'vault'
    delivery_ref = '_system/ledgers/incremental-ingest/run/rounds/round/delivery.json'
    record = {'contract': 'incremental-round-delivery/v1', 'delivery_id': 'delivery-test',
              'revision': 2, 'state': 'build_committing'}
    _write_atomic(_vault_path(vault, delivery_ref), _json_bytes(record))
    _write_atomic(_vault_path(vault, '_system/metadata/incremental-delivery-state.json'), _json_bytes({
        'contract': 'incremental-delivery-state/v1', 'delivery_id': 'delivery-test', 'revision': 2,
        'state': 'build_committing', 'delivery_ref': delivery_ref}))
    monkeypatch.setattr(runtime, '_read_index_status', lambda _: pytest.fail('blocked read initialized Provider models'))
    config = SimpleNamespace(vault_root=vault, vault_id='fixture', progress_path=lambda:tmp_path/'absent',
                             query_config_fingerprint=lambda:'config')
    response = runtime.recall(config, 'question')
    assert response['candidates'] == [] and 'incremental-delivery-in-progress' in response['warnings']
