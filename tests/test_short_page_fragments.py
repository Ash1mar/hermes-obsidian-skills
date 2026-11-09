"""Regression checks for fragment boundaries and citation/QA obligations."""
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

from short_page_contract import check_coordination, check_fragment, obligations, qa_impacts
from short_page_fragments import build, pack


def item(kind, text, section, assets=()):
    return {'kind': kind, 'text': text, 'section': section, 'heading': [section],
            'required_assets': list(assets), 'locator': {'path': f'tables/{section}.md'}}


def link(key, role, *anchors):
    return {'item': key, 'role': role, 'applies_to': list(anchors), 'note': ''}


def source(items):
    return {'items': items, 'qa': {'issues': [], 'review_required': ['table-and-cross-page-structure']},
            'identity': {'resource_id': 'S1'}, 'unit_set_id': 'units', 'document_sha256': 'text',
            'source_sha256': 'source', 'bundle_sha256': 'bundle', 'registry_revision': 1}


def mapped(items, topics):
    return {'topics': {tid: {'source': 'S1', 'identity': tid, 'name': tid, 'scope': 'Scoped', 'relations': relations}
                       for tid, relations in topics.items()},
            'dispositions': {key: {'status': 'linked'} for key in items}}


def page(topics, sections=()):
    return {'name': 'Reader page', 'scope': 'One task', 'topics': topics, 'sections': list(sections),
            'reader_question': 'How do I complete the task?', 'grouping_reason': 'One documented task',
            'grouping_basis': 'single_topic' if len(topics) == 1 else 'shared_reader_task'}


def test_same_topic_splits_without_cutting_assets_or_shared_conditions():
    items = {
        'S1-B1': item('block', 'Applicability: version A only.', 'General'),
        'S1-B2': item('block', '[表](tables/formA.md)', 'Form A', ['S1-A4']),
        'S1-B3': item('block', '[表](tables/formB.md)', 'Form B', ['S1-A5']),
        'S1-A4': item('asset', 'a' * 7500, 'formA'),
        'S1-A5': item('asset', 'b' * 7500, 'formB'),
    }
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [
        link('S1-B1', 'condition'), link('S1-B2', 'asset'), link('S1-B3', 'asset'),
        link('S1-A4', 'asset', 'S1-B2'), link('S1-A5', 'asset', 'S1-B3'),
    ]})
    requests, held, expected = pack(sources, reduced, [page(['T1'])], 40_000)
    assert len(requests) == 2 and not held and len(expected) == 1
    views = [row['view']['pages']['p1'] for row in requests]
    assert [set(view['materials']) for view in views] == [
        {'S1-B1', 'S1-B2', 'S1-A4'}, {'S1-B1', 'S1-B3', 'S1-A5'},
    ]
    assert views[0]['obligations']['S1-B1']['express']
    assert views[1]['obligations']['S1-B1']['assignment'] == 'shared'
    assert all(row['budget']['fits'] for row in requests)


def test_oversized_atomic_part_does_not_block_other_section():
    items = {'S1-A1': item('asset', 'x' * 16000, 'Huge'),
             'S1-B2': item('block', 'Keep one original.', 'Small')}
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-A1', 'asset')], 'T2': [link('S1-B2', 'evidence')]})
    specs = [page(['T1', 'T2'], [{'name': 'Huge', 'topics': ['T1']}, {'name': 'Small', 'topics': ['T2']}])]
    requests, held, expected = pack(sources, reduced, specs, 40_000)
    assert len(requests) == 1 and len(held) == 1
    assert held[0]['assets'] == ['S1-A1']
    answer = {'sections': [{'heading': 'Small', 'content': 'Keep one original. [S1-B2]', 'expressed': ['S1-B2']}],
              'context': [], 'unresolved': [], 'inspected_assets': []}
    receipt = check_fragment(requests[0], 'p1', answer)
    assert receipt['state'] == 'structurally_valid' and receipt['qa_publication_review']
    assert not build(expected, [receipt])


def test_table_wrapper_substitution_and_concrete_format_diagnostic():
    items = {
        'S1-B1': item('block', '**Table p1-1**\n[表](tables/formA.md)', 'one', ['S1-A3']),
        'S1-B2': item('block', '[表](tables/formA.md)\n仅适用于本项目。', 'two', ['S1-A3']),
        'S1-A3': item('asset', 'Full table', 'formA'),
    }
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-B1', 'asset'), link('S1-B2', 'asset'), link('S1-A3', 'asset')]})
    requests, held, expected = pack(sources, reduced, [page(['T1'])], 40_000)
    row, = requests
    view = row['view']['pages']['p1']
    assert not held and view['obligations']['S1-B1']['role'] == 'navigation'
    assert view['obligations']['S1-B2']['express']
    response = {'sections': [{'heading': 'Records', 'content': 'Test. [S1-A3] [S1-B2]',
                              'expressed': ['S1-A3', 'S1-B2']}],
                'context': ['S1-B1'], 'unresolved': [], 'inspected_assets': ['S1-A3']}
    receipt = check_fragment(row, 'p1', response)
    assert len(build(expected, [receipt])) == 1
    response['sections'][0]['content'] = 'Missing inline reference. [S1-A3]'
    with pytest.raises(ValueError, match='declared_without_inline=S1-B2'):
        check_fragment(row, 'p1', response)


def test_qa_explicit_asset_hold_and_unverified_missing_asset_claim():
    items = {'S1-B1': item('block', '<!-- table-id: formA; qa: unreadable -->', 's', ['S1-A2']),
             'S1-A2': {**item('asset', 'Table', 'formA'), 'label': 'tables/formA.md'}}
    sources = {'S1': source(items)}
    duties = obligations(sources, items, {}, set(items))
    impacts = qa_impacts(sources, sources, items, duties)
    assert [(row['writing'], row['items']) for row in impacts] == [('allow', []), ('hold', ['S1-A2'])]
    other = {'S1-B3': item('block', 'Text', 's')}
    planned, _, _ = pack({'S1': source(other)}, mapped(other, {'T1': [link('S1-B3', 'evidence')]}),
                         [page(['T1'])], 40_000)
    receipt = check_fragment(planned[0], 'p1', {'blocked': {
        'kind': 'missing_asset', 'refs': [], 'detail': 'No named asset', 'effect': 'Body blocked',
    }})
    assert receipt['state'] == 'held' and receipt['diagnostic'] == 'no_named_required_asset'


def test_coordinator_needs_a_real_page_reason_field():
    spec = page(['T1', 'T2'])
    check_coordination({'topics': {'T1': {}, 'T2': {}}}, {'pages': [spec], 'unresolved': []})
    with pytest.raises(ValueError, match='grouping reason'):
        check_coordination({'topics': {'T1': {}, 'T2': {}}},
                           {'pages': [{**spec, 'grouping_reason': ' '}], 'unresolved': []})


def test_known_qa_observation_does_not_fill_evidence_or_clear_review():
    items = {'S1-B1': item('block', 'Keep the requirement.', 's')}
    requests, _, _ = pack({'S1': source(items)}, mapped(items, {'T1': [link('S1-B1', 'evidence')]}),
                          [page(['T1'])], 40_000)
    row, = requests
    qa = row['view']['pages']['p1']['qa_impact'][0]['id']
    response = {'sections': [{'heading': 'Requirement', 'content': 'Keep the requirement. [S1-B1]',
                              'expressed': ['S1-B1']}], 'context': [],
                'unresolved': [{'item': qa, 'reason': 'Original QA scope remains unknown.'}], 'inspected_assets': []}
    accepted = check_fragment(row, 'p1', response)
    assert accepted['state'] == 'structurally_valid'
    assert accepted['contract'].endswith('/v2.1')
    assert accepted['qa_publication_review'] == [qa] and accepted['qa_observations'][0]['qa_id'] == qa
    response['sections'][0].update(content='Coverage omitted.', expressed=[])
    with pytest.raises(ValueError, match='required_expressions_missing=S1-B1'):
        check_fragment(row, 'p1', response)
