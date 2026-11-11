"""Content receipts, per-slot limits and complete-section delivery are separate contracts."""
import copy
import json

from test_short_page_fragments import item, link, mapped, page, source
from short_cards import CONTRACT as CARDS
from short_content_recovery import classification, replacement, slot_attempts
from short_page_contract import WRITE_CONTRACT, check_fragment
from short_page_fragments import build, independent_sections, pack


def prepared():
    items = {'S1-B1': item('block', 'Version A definition.', 'One'),
             'S1-B2': item('block', 'Keep exceptions.', 'Two')}
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-B1', 'definition')], 'T2': [link('S1-B2', 'exception')]})
    reduced['writing_contract'] = WRITE_CONTRACT
    reduced['card_layout'] = {'contract': CARDS, 'themes': [], 'directories': [], 'occupied': [], 'existing': {}}
    spec = {**page(['T1', 'T2'], [{'name': '定义', 'topics': ['T1']}, {'name': '例外', 'topics': ['T2']}]),
            'theme_directory': '设计管理'}
    planned, held, expected = pack(sources, reduced, [spec], 40_000)
    return sources, reduced, spec, planned, held, expected


def accepted(row, slot):
    view = row['view']['pages'][slot]
    refs = list(view['materials'])
    return check_fragment(row, slot, {'sections': [{'heading': '原文要求', 'content': ' '.join(f'规则 [{ref}]' for ref in refs)}],
                                     'context': [], 'unresolved': [], 'inspected_assets': []})


def test_only_whole_independent_sections_escape_a_failed_parent():
    sources, reduced, spec, planned, held, expected = prepared()
    row = planned[0]
    slot = next(slot for slot, view in row['view']['pages'].items() if 'S1-B1' in view['materials'])
    check = accepted(row, slot)
    assert not build(expected, [check])
    delivered = independent_sections(expected, [check], [], [spec], reduced, planned, held)
    assert len(delivered) == 2
    pages = build(delivered, [check])
    child, = pages.values()
    assert child['name'] == '定义' and child['card_path'] == '30_Cards/设计管理/定义.md'
    assert child['derived_from']['receipt_sha256'] == [check['receipt_sha256']]
    assert child['fragment_receipts'] == [check['receipt_sha256']]
    assert '例外' not in child['content']
    assert len(independent_sections(expected, [check], [], [{**spec, 'grouping_basis': 'inseparable_conditions'}],
                                    reduced, planned, held)) == 1


def test_a_missing_part_of_the_same_section_cannot_be_salvaged():
    _, reduced, spec, planned, held, expected = prepared()
    checks = [accepted(planned[0], 'p1')]
    expected = copy.deepcopy(expected)
    parent, = expected.values()
    parent['fragments'].append('oversized')
    held = [{'fragment_id': 'oversized', 'position': checks[0]['fragment_position']}]
    delivered = independent_sections(expected, checks, [], [spec], reduced, planned, held)
    assert delivered == expected


def test_content_repair_preserves_definition_and_uses_only_local_question():
    sources, _, _, planned, _, _ = prepared()
    row = planned[0]
    slot = 'p1'
    view = row['view']['pages'][slot]
    prior = check_fragment(row, slot, {'blocked': {'kind': 'missing_context', 'refs': list(view['materials']),
                                                 'detail': 'Missing sibling section.', 'effect': 'Cannot answer full page.'}})
    new = replacement(row, slot, prior, 40_000, sources)
    updated = new['view']['pages']['p1']
    assert updated['materials'] == view['materials'] and updated['obligations'] == view['obligations']
    assert 'reader_question' not in updated and updated['name'] == view['fragment_position']['name']
    assert updated['repair_of_fragment'] == view['fragment_id']
    assert updated['content_recovery']['maximum_additional_calls'] == 1
    assert 'single content recovery opportunity' in new['system']
    unreadable = {**prior, 'answer': {'blocked': {'kind': 'unreadable', 'detail': 'Unknown glyphs.'}}}
    assert classification(view, unreadable, sources) == 'source_quality_deferred'


def test_reservations_count_per_slot_and_include_unknown_descendant(tmp_path):
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    for number, pages in enumerate(({'p1': {'fragment_id': 'a'}, 'p2': {'fragment_id': 'b'}},
                                   {'p2': {'fragment_id': 'b'}},
                                   {'p1': {'fragment_id': 'c', 'repair_of_fragment': 'a'}})):
        (jobs / f'job{number}.attempt-1.json').write_text(json.dumps({'request': {'view': {'pages': pages}}}))
    assert len(slot_attempts(tmp_path, 'a')) == 2
    assert len(slot_attempts(tmp_path, 'b')) == 2
    assert len(slot_attempts(tmp_path, 'c')) == 1
