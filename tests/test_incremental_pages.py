"""Incremental revisions preserve registered identity, parents and original evidence."""
import copy
from pathlib import Path

import pytest

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import ContractError, FileVaultFinalizeService
from hermes_source_units import incremental_pages as pages, short_build
from hermes_source_units.build_acceptance import assess, record
from hermes_source_units.page_governance import POLICY
from hermes_source_units.source_units import _json_bytes, _load_json, _sha
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


def test_page_preparation_binds_aliases_but_allows_unrelated_registry_revision(vault):
    refs = publish_sources(vault, ['# A\n\nFirst required condition.\n', '# B\n\nSecond required condition.\n'])
    service = FileVaultFinalizeService(vault).build
    raw = decision('create', 'combined/scope', '30_Cards/工程/综合.md')
    raw['support_aliases'] = ['E1', 'E2']
    change, = pages.normalize_page_changes(service, round_input(1), {'changes': [raw]},
                                           {'E1': refs[0], 'E2': refs[1]})
    prepared = pages.prepare_page_revision(service, change, 'agent')
    response = {'content': '# Conditions\n\nFirst [m1]. Second [m2].',
                'inspected': list(prepared['material_aliases']), 'unresolved': []}
    pages.validate_page_revision(prepared, response)
    registry_path = vault / '_system/metadata/document-registry.json'
    registry = _load_json(registry_path)
    registry['registry_revision'] += 1
    registry_path.write_bytes(_json_bytes(registry))
    pages.validate_preparation(service, prepared, 'agent')
    tampered = copy.deepcopy(prepared)
    keys = list(tampered['material_aliases'])
    first, second = keys[:2]
    tampered['material_aliases'][first], tampered['material_aliases'][second] = (
        tampered['material_aliases'][second], tampered['material_aliases'][first])
    with pytest.raises(ContractError, match='preparation fingerprint'):
        pages.validate_page_revision(tampered, response)


def test_incremental_fragment_budget_coverage_and_parent_bytes(vault, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] /
                                  'hermes-obsidian-governed-ingest-orchestrator' / 'lib'))
    from incremental_writing import assemble, prepare_work
    from incremental_writing_evidence import EvidenceCatalogue

    prior, left, right = publish_sources(vault, [
        'Original condition.\n',
        'First original condition applies. ' * 55 + '\n',
        'Second original condition applies. ' * 55 + '\n',
    ])
    service = FileVaultFinalizeService(vault).build
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {'task_id': 'fixture', 'run_id': 1})
    first, = pages.normalize_page_changes(service, round_input(1), {'changes': [
        decision('create', 'inspection/fragmented', '30_Cards/工程/检查要求.md')]}, {'E1': prior})
    old_body = b'# Requirements\n\nOriginal condition.\n'
    old, _ = admit(service, tmp_path / 'first', first, old_body)
    parent, = finalize(service, old)['page_revisions']
    raw = decision('update', '', parent['path'], parent['page_id'])
    raw['support_aliases'] = ['left', 'right']
    plan, = pages.normalize_page_changes(service, round_input(2), {'changes': [raw]}, {'left': left, 'right': right})
    maps = []
    for alias, ref in [('left', left), ('right', right)]:
        key = ref['unit_ref']
        _, units, _ = service.source._load_set(key['resource_id'], key['unit_set_id'])
        unit = next(row for row in units if row['ref'] == key)
        maps.append({'snapshot_id': alias, 'map_result_id': f'map-{alias}',
                     'alias_table': {alias: {'unit_ref': key, 'snapshot_id': alias, 'sha256': unit['content_sha256']}},
                     'coverage': [{'unit_ref': key, 'disposition': 'mapped'}], 'mapped': {'topics': {}}})
    catalogue = EvidenceCatalogue(service, maps, 'agent')
    whole = prepare_work(service, catalogue, plan)
    assert len(whole['fragments']) == 1
    assert '[m1][m2]' in whole['fragments'][0]['request']['messages']
    assert 'context_truncated flag alone' in whole['fragments'][0]['request']['messages']
    legacy = prepare_work(service, catalogue, plan, writing_revision=1)
    assert 'writing_revision' not in legacy
    assert '[m1][m2]' not in legacy['fragments'][0]['request']['messages']
    cap = whole['whole_cost']['input_size'] - 1
    work = prepare_work(service, catalogue, plan, input_cap=cap)
    assert len(work['fragments']) == 2
    assert not work['whole_cost']['fits']
    answers = []
    for fragment in work['fragments']:
        assert fragment['request']['budget']['fits']
        prepared = fragment['prepared']
        alias, = prepared['view']['assigned']
        answers.append({'content': f'## Condition\n\nApplicable condition [{alias}].',
                        'inspected': list(prepared['material_aliases']), 'unresolved': []})
    result = assemble(service, work, answers)
    assert result['body'].startswith(old_body) and result['body'].count(b'## Condition') == 2
    assert sorted(result['citation_bindings']) == ['m1', 'm2']
    assert work['change']['page_id'] == parent['page_id']
    assert work['change']['expected_parent_revision_id'] == parent['revision_id']
    assert result['support_refs'][0] == prior
    with pytest.raises(ContractError, match='unresolved'):
        assemble(service, work, [answers[0], {**answers[1], 'unresolved': ['missing table']}])


def test_incremental_heading_without_registered_body_does_not_use_parallel_source(vault, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] /
                                  'hermes-obsidian-governed-ingest-orchestrator' / 'lib'))
    from incremental_writing import prepare_work
    from incremental_writing_evidence import EvidenceCatalogue

    refs = publish_sources(vault, [
        '# 2.1 术语\n',
        '# 2.1 术语\n\n## 2.1.1 系统\n\nAn unrelated version defines a different system.\n',
        '# 5.0.1 设计参数不应低于 10。\n',
    ])
    service = FileVaultFinalizeService(vault).build
    maps = []
    for index, ref in enumerate(refs, 1):
        key = ref['unit_ref']
        _, units, _ = service.source._load_set(key['resource_id'], key['unit_set_id'])
        snapshot = f'snapshot-{index}'
        maps.append({'snapshot_id': snapshot, 'map_result_id': f'map-{index}',
                     'alias_table': {f'E{index}-{i}': {'unit_ref': unit['ref'], 'snapshot_id': snapshot,
                                                     'sha256': unit['content_sha256']}
                                     for i, unit in enumerate(units)},
                     'coverage': [{'unit_ref': unit['ref'], 'disposition': 'mapped'} for unit in units],
                     'mapped': {'topics': {}}})
    catalogue = EvidenceCatalogue(service, maps, 'agent')
    intent = decision('create', 'terms', '30_Cards/工程/术语.md')
    intent['name'] = '术语'
    plan, = pages.normalize_page_changes(service, round_input(1), {'changes': [intent]}, {'E1': refs[0]})
    with pytest.raises(ContractError, match='no mapped original body in the registered section 2.1 术语'):
        prepare_work(service, catalogue, plan)

    plan, = pages.normalize_page_changes(service, round_input(2), {'changes': [intent]}, {'E1': refs[1]})
    work = prepare_work(service, catalogue, plan)
    assert len(work['selection']['located_sections']) == 1
    fragment, = work['fragments']
    assert fragment['request']['view']['navigation_headings'] == ['2.1 术语']
    assert refs[1] not in fragment['prepared']['support_refs']

    plan, = pages.normalize_page_changes(service, round_input(3), {'changes': [intent]}, {'E1': refs[2]})
    work = prepare_work(service, catalogue, plan)
    fragment, = work['fragments']
    assert fragment['prepared']['support_refs'] == [refs[2]]


def test_coordination_projection_preserves_scoped_relations_and_sealed_registry(monkeypatch):
    frozen = round_input(1)
    frozen['context_map_result_ids'] = ['map-0']
    frozen['snapshots'].append('snapshot-0')
    native = {'map_result_id': 'map-1', 'snapshot_id': 'snapshot-1',
              'alias_table': {key: {} for key in ('S1-B1', 'S1-B2', 'S1-B3')},
              'coverage': ['control-only'], 'native_completions': ['control-only'],
              'mapped': {'topics': {'T1': {'identity': 'native-topic-identity', 'name': 'Scope A',
                  'scope': 'One source scope', 'source': 'S1', 'relations': [
                      {'item': 'S1-B1', 'role': 'evidence', 'applies_to': [], 'note': 'Pinned evidence prose'},
                      {'item': 'S1-B2', 'role': 'condition', 'applies_to': ['S1-B1', 'S1-B3'],
                       'note': 'Required condition'}]}}}}
    context = {'map_result_id': 'map-0', 'snapshot_id': 'snapshot-0', 'mapped': {'topics': {
        'T1': {'identity': 'other-native-identity', 'name': 'Scope B', 'scope': 'Prior scope',
               'source': 'S2', 'relations': [{'item': 'S2-B1', 'role': 'evidence', 'applies_to': [],
                                            'note': 'Prior content is not new support'}]}}}}
    registered = [{'page_id': 'stable-page', 'path': '30_Cards/Stable.md', 'page_type': 'card',
                   'revision_id': 'exact-parent', 'support_refs': ['control-only-old-evidence'],
                   'identity': {'kind': 'concept', 'identity_key': 'scoped-identity',
                                'canonical_name': 'Stable', 'aliases': []}}]
    original = copy.deepcopy([native, context])
    topics = pages.coordination_catalogue(frozen, [native, context])
    assert len(topics) == 1 and topics[0]['identity'] == 'native-topic-identity'
    assert topics[0]['relations'][0][3] == 'Pinned evidence prose'
    assert pages.group_aliases(topics) == ['S1-B1', 'S1-B2', 'S1-B3']
    monkeypatch.setattr(pages, 'registry_view', lambda *_: pytest.fail('must use the sealed registry'))
    spec = pages.plan_page_changes(object(), frozen, [native, context], topics=topics,
                                   registered_pages=registered)
    view = spec['view']
    assert view['mapping_sources'] == [{'map_result_id': 'map-1', 'snapshot_id': 'snapshot-1'}]
    assert view['mapping_topics'][0]['relations'] == [
        ['S1-B1', 'evidence', [], ''], ['S1-B2', 'condition', ['S1-B1', 'S1-B3'], 'Required condition']]
    assert 'identity' not in view['mapping_topics'][0]
    assert view['context_topics'][0]['topics'] == [
        {'topic_id': 'T1', 'name': 'Scope B', 'scope': 'Prior scope', 'source': 'S2'}]
    assert view['registered_pages'][0]['page_id'] == 'stable-page'
    assert view['registered_pages'][0]['revision_id'] == 'exact-parent'
    assert view['registered_page_snapshot_sha256'] == fingerprint(registered)
    assert 'control-only' not in repr(view)
    assert [native, context] == original
    clarified = pages.plan_page_changes(object(), frozen, [native, context], topics=topics,
                                        registered_pages=registered, planning_revision=2)['view']
    assert clarified['planning_revision'] == 2
    assert clarified['mapping_topics'] == view['mapping_topics']
    assert 'allowed_output_paths' in clarified and 'planning_revision' not in view
    legacy = pages.plan_page_changes(object(), frozen, [native, context], registered_pages=registered)
    assert legacy['view']['mapping_results'] == [native, context]
    assert legacy['view']['registered_pages'] == registered


def test_coordination_group_rejects_partial_topics_and_unexposed_aliases():
    frozen = round_input(1)
    mapped = {'map_result_id': 'map-1', 'snapshot_id': 'snapshot-1', 'alias_table': {'S1-B1': {}},
              'mapped': {'topics': {'T1': {'identity': 'original-topic', 'name': 'One', 'scope': 'Scope',
                  'source': 'S1', 'relations': [
                      {'item': 'S1-B1', 'role': 'evidence', 'applies_to': [], 'note': ''}]}}}}
    catalogue = pages.coordination_catalogue(frozen, [mapped])
    partial = copy.deepcopy(catalogue)
    partial[0]['relations'] = []
    with pytest.raises(ContractError, match='exact complete frozen topics'):
        pages.plan_page_changes(object(), frozen, [mapped], topics=partial, registered_pages=[])
    mapped['mapped']['topics']['T1']['relations'][0]['applies_to'] = ['S1-B2']
    with pytest.raises(ContractError, match='outside its verified Map'):
        pages.coordination_catalogue(frozen, [mapped])
