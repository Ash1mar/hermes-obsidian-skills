"""Incremental revisions preserve registered identity, parents and original evidence."""
from pathlib import Path

import pytest

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import ContractError, FileVaultFinalizeService
from hermes_source_units import incremental_pages as pages, short_build
from hermes_source_units.build_acceptance import assess, record
from hermes_source_units.page_governance import POLICY
from hermes_source_units.source_units import _json_bytes, _sha
from hermes_source_units.validation import fingerprint


def round_input(index):
    return {'contract': 'incremental-round-input/v1', 'run_id': 'incremental-test',
            'round_id': f'round-{index}', 'map_result_ids': [f'map-{index}'],
            'snapshots': [f'snapshot-{index}'], 'input_fingerprint': fingerprint(index)}


def decision(action, key, path, page_id=''):
    return {'action': action, 'page_id': page_id, 'identity_key': key, 'name': 'Inspection requirements',
            'path': path, 'page_type': 'card', 'support_aliases': ['E1'],
            'required_source_snapshots': [], 'reason': 'Full declared evidence for this scope'}


def admit(service, root, change, body):
    authorization = {'contract': pages.AUTHORIZATION, 'actor': 'agent', 'run_id': change['run_id'],
                     'round_id': change['round_id'], 'pages': [change['change_id']], 'page_changes': [change],
                     'base_release': None, 'publish': True, 'policy': POLICY}
    operation = {'vault': str(service.vault), 'origin': {'snapshots': change['input_source_snapshots']},
                 'workflow_mode': 'incremental_v1', 'incremental': {'run_id': change['run_id'],
                                                                'scope_id': change['round_id']}}
    root.mkdir()
    (root / 'operation.json').write_bytes(_json_bytes(operation))
    job = root / 'job.json'
    job.write_bytes(_json_bytes({'request': {'phase': 'incremental-formal-build',
                                           'view': {'authorization': authorization}}}))
    row = {'short_id': change['change_id'], 'name': change['name'], 'incremental_change': change,
           'page_type': change['page_type'], 'card_path': change['path'], 'authored_sha256': _sha(body),
           'support_refs': pages.unique_refs([*change['retained_support_refs'], *change['added_support_refs']]),
           'native_fragments': [{'receipt_sha256': fingerprint(body.decode())}], 'qa_issues': [],
           'qa_status': 'usable', 'publication_authorized': True, 'source_eligible_at_admission': True}
    scope = {'actor': 'agent', 'authorization': authorization, 'origin': operation['origin'],
             'coverage_complete': False, 'operation_sha256': _sha((root / 'operation.json').read_bytes()),
             'pages': [row], 'excluded': []}
    run = short_build.admit(service, scope, {change['change_id']: body}, root, job)
    return run, scope


def finalize(service, run, parent=None):
    reviews = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'],
                'parent_authored_sha256': parent, 'actor': 'agent', 'note': 'Verified native evidence.',
                'acceptance': record(assess(service, run, p))} for p in run['page_revisions']]
    return service.finalize({'run_id': run['run_id'], 'actor': 'agent',
                             'expected_revision': run['revision'], 'reviews': reviews})['run']


def test_same_page_adds_source_with_exact_parent_and_preserves_old_original(vault, tmp_path, monkeypatch):
    refs = publish_sources(vault, ['# A\n\nFirst required condition.\n', '# B\n\nAnother required condition.\n'])
    service = FileVaultFinalizeService(vault).build
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {'task_id': 'fixture', 'run_id': 1})
    first, = pages.normalize_page_changes(service, round_input(1), {'changes': [
        decision('create', 'inspection/scope-a', '30_Cards/工程/检查要求.md')]}, {'E1': refs[0]})
    original, _ = admit(service, tmp_path / 'first', first, b'# Requirements\n\nFirst condition [m1].\n')
    committed = finalize(service, original)
    p1, = committed['page_revisions']

    second, = pages.normalize_page_changes(service, round_input(2), {'changes': [
        decision('update', '', p1['path'], p1['page_id'])]}, {'E1': refs[1]})
    assert second['expected_parent_revision_id'] == p1['revision_id']
    assert second['retained_support_refs'] == [refs[0]] and second['added_support_refs'] == [refs[1]]
    writer = pages.prepare_page_revision(service, second, 'agent')
    assert 'First condition' in writer['view']['parent_content']
    assert writer['support_refs'] == refs
    updated, _ = admit(service, tmp_path / 'second', second,
                       b'# Requirements\n\nFirst condition [m1]. Another condition [m2].\n')
    p2, = finalize(service, updated, p1['authored_sha256'])['page_revisions']
    assert p2['page_id'] == p1['page_id'] and p2['parent_revision_id'] == p1['revision_id']
    assert p2['revision_id'] != p1['revision_id'] and p2['support_refs'] == refs
    with pytest.raises(ContractError, match='parent changed'):
        pages.prepare_page_revision(service, second, 'agent')


def test_same_title_distinct_scope_is_not_merged_and_unready_dependency_is_held(vault):
    ref, = publish_sources(vault, ['# Source\n\nEvidence.\n'])
    service = FileVaultFinalizeService(vault).build
    a = decision('create', 'building/scope-a', '30_Cards/工程/A.md')
    b = decision('create', 'building/scope-b', '30_Cards/工程/B.md')
    changes = pages.normalize_page_changes(service, round_input(1), {'changes': [a, b]}, {'E1': ref})
    assert changes[0]['page_id'] != changes[1]['page_id']
    a['required_source_snapshots'] = ['not-yet-ready']
    held, = pages.normalize_page_changes(service, round_input(1), {'changes': [a]}, {'E1': ref})
    assert held['action'] == 'hold' and held['unresolved'] == [{'missing_snapshot': 'not-yet-ready'}]


def test_legacy_untyped_short_page_keeps_its_id_path_and_absent_type(vault, tmp_path, monkeypatch):
    ref, = publish_sources(vault, ['# A\n\nA complete original condition.\n'])
    service = FileVaultFinalizeService(vault).build
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {'task_id': 'fixture', 'run_id': 1})
    root = tmp_path / 'legacy'
    root.mkdir()
    operation = {'vault': str(vault), 'origin': {'workflow_id': 'legacy-origin'}}
    (root / 'operation.json').write_bytes(_json_bytes(operation))
    key = fingerprint('legacy-page')
    grant = {'actor': 'agent', 'pages': [key], 'publish': True, 'policy': POLICY}
    job = root / 'job.json'
    job.write_bytes(_json_bytes({'request': {'phase': 'short-formal-build', 'view': {'authorization': grant}}}))
    body = b'# Legacy\n\nOriginal condition [m1].\n'
    candidate = {'short_id': key, 'name': 'Legacy', 'authored_sha256': _sha(body), 'support_refs': [ref],
                 'native_fragments': [{'receipt_sha256': fingerprint('original')}], 'qa_issues': [],
                 'qa_status': 'usable', 'publication_authorized': True, 'source_eligible_at_admission': True}
    scope = {'actor': 'agent', 'authorization': grant, 'origin': operation['origin'], 'coverage_complete': False,
             'operation_sha256': _sha((root / 'operation.json').read_bytes()), 'pages': [candidate], 'excluded': []}
    run = short_build.admit(service, scope, {key: body}, root, job)
    p1, = finalize(service, run)['page_revisions']
    assert 'page_type' not in p1
    change, = pages.normalize_page_changes(service, round_input(1), {'changes': [
        decision('update', '', p1['path'], p1['page_id'])]}, {'E1': ref})
    assert change['legacy_untyped'] is True
    update, _ = admit(service, tmp_path / 'new', change, b'# Legacy\n\nComplete condition [m1].\n')
    p2, = finalize(service, update, p1['authored_sha256'])['page_revisions']
    assert 'page_type' not in p2
    assert p2['path'] == p1['path'] and p2['page_id'] == p1['page_id']
