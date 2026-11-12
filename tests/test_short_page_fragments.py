"""Regression checks for fragment boundaries and citation/QA obligations."""
import json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

from short_page_contract import (WRITE_CONTRACT, check_coordination, check_fragment, obligations, qa_impacts,
                                 restore_locked_draft, table_navigation, writing_retry)
from short_page_fragments import build, pack
from short_fragment_repair import equivalent_coverage, form_navigation
from short_fragment_disposition import exact_continuations, verify_block
from hermes_source_units.validation import fingerprint


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


def test_new_card_keeps_definition_and_limits_a_local_table_hold():
    from short_cards import CONTRACT

    items = {'S1-B1': item('block', 'A design record means the required document.', 'Section'),
             'S1-B2': item('block', 'Inspection conditions are in the table.', 'Section', ['S1-A3']),
             'S1-A3': item('asset', '| Table condition |', 'Section')}
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-B1', 'definition'), link('S1-B2', 'evidence')]})
    reduced['writing_contract'] = WRITE_CONTRACT
    layout = {'contract': CONTRACT, 'themes': ['设计管理'], 'directories': [], 'occupied': [], 'existing': {}}
    spec = {**page(['T1']), 'name': '设计文件定义', 'theme_directory': '设计管理'}
    old, _, _ = pack(sources, reduced, [spec], 40_000)
    assert not old[0]['view']['pages']['p1']['obligations']['S1-B1']['express']
    reduced['card_layout'] = layout
    reduced['dispositions']['S1-A3']['status'] = 'pending'
    requests, held, expected = pack(sources, reduced, [spec], 40_000)
    assert len(held) == 1 and held[0]['unresolved'] == ['S1-A3']
    row, = requests
    view = row['view']['pages']['p1']
    assert view['obligations']['S1-B1']['express'] and set(view['materials']) == {'S1-B1'}
    answer = {'sections': [{'heading': '设计文件', 'content': '定义 [S1-B1]'}],
              'context': [], 'unresolved': [], 'inspected_assets': []}
    accepted = check_fragment(row, 'p1', answer)
    assert not build(expected, [accepted])
    with pytest.raises(ValueError, match='required_expressions_missing=S1-B1'):
        check_fragment(row, 'p1', {**answer, 'context': ['S1-B1'],
                                   'sections': [{'heading': '定义', 'content': '定义'}]})


def test_card_path_is_readable_safe_and_stable_and_renders_toc():
    from short_cards import CONTRACT, place, render

    layout = {'contract': CONTRACT, 'themes': ['Design'], 'directories': ['design'],
              'occupied': ['30_Cards/Design/表单－校核.md'], 'existing': {'old': '20_Notes/ShortEvidence/old.md'}}
    first = {'id': '12345678a', 'name': '表单/校核', 'theme_directory': 'design'}
    second = {'id': '23456789b', 'name': '表单/校核', 'theme_directory': 'Design'}
    old = {'id': 'old', 'name': 'Old', 'theme_directory': 'Design'}
    rows = place(layout, [first, second, old])
    assert rows == list(reversed(place(layout, [old, second, first])))
    assert rows[0]['card_path'] == '30_Cards/Design/表单－校核 · 12345678.md'
    assert rows[1]['card_path'] == '30_Cards/Design/表单－校核 · 23456789.md'
    assert rows[2]['registered_path'] == layout['existing']['old']
    text = render(first, {(1, '适用范围'): [{'heading': '适用范围', 'content':
                  '###### 额外说明\n应核对 [S1-B1]\n\n\x60\x60\x60\n# literal\n\x60\x60\x60'}]})
    assert 'type: knowledge-card' in text and '# 表单/校核\n' in text
    assert '## 目录' in text and '## 适用范围\n' in text and '### 适用范围（2）' in text
    assert '#### 额外说明' in text and '# literal' in text and '[S1-B1]' in text


def test_typed_plan_holds_only_the_project_without_source_basis():
    from hermes_source_units.short_build import TYPED_LAYOUT
    from short_page_contract import coordination_request

    items = {'S1-B1': item('block', '北岸工程须提交设计文件。', 'Project'),
             'S1-B2': item('block', '设计输出应保留版本信息。', 'Topic')}
    sources = {'S1': source(items)}
    reduced = {**mapped(items, {'T1': [link('S1-B1', 'evidence')], 'T2': [link('S1-B2', 'evidence')]}),
               'unresolved': [], 'writing_contract': WRITE_CONTRACT,
               'card_layout': {'contract': TYPED_LAYOUT, 'themes': [], 'directories': [],
                               'occupied': [], 'existing': {}}}
    coordinate = coordination_request(sources, reduced)
    assert 'allow_syntheses' not in coordinate['view']
    assert coordinate['view']['topics']['T1']['project_text_refs'] == ['S1-B1']
    topic = {**page(['T2']), 'name': '设计输出', 'theme_directory': '设计', 'page_type': 'topic',
             'project_basis': None}
    project = {**page(['T1']), 'name': '北岸工程交付', 'theme_directory': '设计', 'page_type': 'project',
               'project_basis': {'name': '北岸工程', 'refs': ['S1-B2']}}
    check_coordination(coordinate['view'], {'pages': [topic, project], 'unresolved': []})
    requests, held, expected = pack(sources, reduced, [topic, project], 40_000)
    row, = requests
    assert len(held) == 1 and held[0]['reason'] == 'project_basis_not_bound_to_mapped_source'
    answer = {'sections': [{'heading': '版本要求', 'content': '应保留版本信息 [S1-B2]'}],
              'context': [], 'unresolved': [], 'inspected_assets': []}
    delivered, = build(expected, [check_fragment(row, 'p1', answer)]).values()
    assert delivered['card_path'] == '50_Projects/专题/设计输出.md'
    assert delivered['page_type'] == 'topic' and 'project_basis' not in delivered

    project['project_basis']['refs'] = ['S1-B1']
    requests, held, expected = pack(sources, reduced, [topic, project], 40_000)
    assert not held
    view = next(view for row in requests for view in row['view']['pages'].values()
                if view['page_type'] == 'project')
    assert view['project_basis_refs'] == ['S1-B1']
    assert view['obligations']['S1-B1']['express']


def test_shared_synthesis_reuses_evidence_without_owning_the_entries(tmp_path):
    from hermes_source_units.short_build import TYPED_LAYOUT
    from prepare_short_evidence_test import advance_fragments, seal
    from short_knowledge_plan import CONTRACT

    items = {
        'S1-B1': item('block', 'Retain design revision.', 'Versions'),
        'S1-B2': item('block', 'Check the revision before acceptance.', 'Acceptance'),
        'S1-B3': item('block', 'Applies only to approved revisions.', 'Conditions'),
        'S1-B4': item('block', 'Archive transmittals under their local reference.', 'Archive'),
    }
    sources = {'S1': source(items)}
    reduced = {**mapped(items, {'T1': [link('S1-B1', 'evidence'), link('S1-B3', 'condition', 'S1-B1'),
                                      link('S1-B4', 'evidence')],
                               'T2': [link('S1-B2', 'evidence')]}),
               'unresolved': [], 'writing_contract': WRITE_CONTRACT,
               'card_layout': {'contract': TYPED_LAYOUT, 'knowledge_plan': CONTRACT, 'local_fragment_scope': True,
                               'themes': [], 'directories': [], 'occupied': [], 'existing': {}}}
    coord = advance_fragments(sources, reduced, tmp_path, 40_000, {}, [])
    row, = coord['requests']
    assert coord['stage'] == 'coordination' and row['view']['allow_syntheses']
    entries = [{**page([key]), 'name': name, 'theme_directory': '设计', 'page_type': 'card', 'project_basis': None}
               for key, name in (('T1', '版本保留'), ('T2', '验收核对'))]
    synthesis = {'name': '版本和验收的关系', 'scope': '已批准版本', 'reader_question': '版本如何约束验收？',
                 'theme_directory': '设计', 'anchors': [{'topic': 'T1', 'ref': 'S1-B1'}, {'topic': 'T2', 'ref': 'S1-B2'}]}
    invalid = {**synthesis, 'name': '越界依据',
               'anchors': [synthesis['anchors'][0], {'topic': 'T2', 'ref': 'S1-B4'}]}
    seal(coord, row['id'], {'results': {'r': {'pages': entries, 'unresolved': [], 'syntheses': [synthesis, invalid]}}}, tmp_path)
    plan = advance_fragments(sources, reduced, tmp_path, 40_000, {}, [])
    assert plan['stage'] == 'writing' and len(plan['expected_pages']) == 3
    assert plan['coordination_issues'][0]['reason'] == 'synthesis_anchor_lacks_independent_owner_or_mapped_evidence'
    ready, topic_id, synthesis_receipt = [], None, None
    for request in plan['requests']:
        for slot, view in request['view']['pages'].items():
            if 'synthesis_task' in view:
                topic_id = view['id']
                assert set(view['materials']) == {'S1-B1', 'S1-B2', 'S1-B3'}
                assert view['fragment_task']['question'] == synthesis['reader_question']
                missing = {'sections': [{'heading': '关系', 'content': '[S1-B1] [S1-B2]'}],
                           'context': ['S1-B3'], 'unresolved': [], 'inspected_assets': []}
                with pytest.raises(ValueError, match='required_expressions_missing=S1-B3'):
                    check_fragment(request, slot, missing)
                synthesis_receipt = check_fragment(request, slot, {
                    **missing, 'context': [],
                    'sections': [{'heading': '关系', 'content': 'Only approved revisions [S1-B3] connect '
                                  'retention [S1-B1] with acceptance [S1-B2].'}]})
                continue
            ready.append(check_fragment(request, slot, {
                'sections': [{'heading': '要求', 'content': ' '.join(f'[{ref}]' for ref in view['materials'])}],
                'context': [], 'unresolved': [], 'inspected_assets': []}))
    completed = build(plan['expected_pages'], ready)
    assert topic_id not in completed and len(completed) == 2
    assert all(p['page_type'] == 'card' for p in completed.values())
    assert {row['short_id'] for row in plan['expected_pages'][topic_id]['synthesis_basis']['entries']} == completed.keys()
    topic = build(plan['expected_pages'], [*ready, synthesis_receipt])[topic_id]
    assert topic['page_type'] == 'topic' and topic['card_path'].startswith('50_Projects/专题/')
    assert '[S1-B3]' in topic['content'] and '[S1-B4]' not in topic['content']


def test_oversized_coordination_keeps_exact_primary_topic_partition(tmp_path):
    from hermes_source_units.short_build import TYPED_LAYOUT
    from prepare_short_evidence_test import advance_fragments
    from short_knowledge_plan import CONTRACT
    from short_page_contract import coordination_request

    items = {f'S1-B{i}': item('block', f'Evidence {i}.', 'Section') for i in range(1, 37)}
    sources = {'S1': source(items)}
    reduced = {**mapped(items, {f'T{i}': [link(ref, 'evidence')]
                               for i, ref in enumerate(items, 1)}), 'unresolved': [],
               'card_layout': {'contract': TYPED_LAYOUT, 'knowledge_plan': CONTRACT, 'themes': []}}
    assert not coordination_request(sources, reduced)['budget']['fits']
    plan = advance_fragments(sources, reduced, tmp_path, 40_000, {}, [])
    assert plan['stage'] == 'coordination'
    rows = plan['requests']
    assert len(rows) > 1 and all(row['budget']['fits'] for row in rows)
    members = [key for row in rows for key in row['view']['topics']]
    assert len(members) == len(set(members)) and set(members) == set(reduced['topics'])


def test_coordination_keeps_primary_page_when_optional_dispositions_fail():
    from short_knowledge_plan import SCOPE_CONTRACT
    from short_page_contract import scoped_coordination_result

    ownership = {'only_navigation': False, 'primary_refs': ['S1-B1'], 'navigation_refs': {}}
    view = {'scope_contract': SCOPE_CONTRACT, 'topics': {key: {'evidence_ownership': ownership}
                                                       for key in ('T1', 'T2')}}
    draft = page(['T1'], [{'name': 'First', 'topics': ['T1']}, {'name': 'Second', 'topics': ['T1']}])
    response = {'pages': [draft], 'unresolved': [], 'navigation_only': [{'topics': ['T2'], 'reason': 'Only index'}]}
    result = scoped_coordination_result(view, response)
    assert result['pages'] == [{**draft, 'sections': []}]
    assert not result['navigation_only'] and result['navigation_holds'][0]['topics'] == ['T2']
    assert result['discarded_section_layouts'][0]['topics'] == ['T1']
    assert len(response['pages'][0]['sections']) == 2


def test_page_scope_survives_context_wrapper_and_keeps_anchored_exception():
    from hermes_source_units.short_build import TYPED_LAYOUT
    from short_knowledge_plan import CONTRACT, SCOPE_CONTRACT
    from short_fragment_repair import scoped_request

    items = {'S1-B1': item('block', 'Keep the revision.', 'Revision', ['S1-A4']),
             'S1-B2': item('block', 'See both tables.', 'Wrapper', ['S1-A4', 'S1-A5']),
             'S1-B3': item('block', 'Except withdrawn versions.', 'Limit'),
             'S1-A4': item('asset', '| Revision |', 'Revision table'),
             'S1-A5': item('asset', '| Reviews |', 'Review table')}
    sources = {'S1': source(items)}
    reduced = {**mapped(items, {'T1': [link('S1-B1', 'evidence')],
                               'T2': [link('S1-B2', 'context', 'S1-B1'),
                                      link('S1-B3', 'exception', 'S1-B1')]}),
               'bounded_coordination': True, 'writing_contract': WRITE_CONTRACT,
               'card_layout': {'contract': TYPED_LAYOUT, 'knowledge_plan': CONTRACT, 'scope_contract': SCOPE_CONTRACT,
                               'local_fragment_scope': True, 'themes': [], 'directories': [],
                               'occupied': [], 'existing': {}}}
    reduced['topics']['T2']['scope'] = 'Wide review procedure'
    spec = {**page(['T1']), 'name': 'Revision', 'theme_directory': 'Design', 'page_type': 'card'}
    requests, held, _ = pack(sources, reduced, [spec], 40_000)
    row, = requests
    assert not held
    view = row['view']['pages']['p1']
    assert view['reader_question'] == spec['reader_question']
    assert view['fragment_task']['deliverable_scope'] == [spec['scope']]
    assert view['materials']['S1-B2']['required_assets'] == ['S1-A4'] and 'S1-A5' not in view['materials']
    assert not view['obligations']['S1-B2']['express']
    assert view['obligations']['S1-B3']['express'] and view['obligations']['S1-A4']['inspect']
    retry = scoped_request(row, 'p1', 40_000, modern=True)['view']['pages']['p1']
    assert retry['fragment_task']['deliverable_scope'] == [spec['scope']]
    assert not retry['obligations']['S1-B2']['covers_by']


def test_knowledge_plan_records_existing_match_without_using_title_as_identity():
    from hermes_source_units.short_build import TYPED_LAYOUT
    from short_knowledge_plan import CONTRACT
    from short_page_fragments import spec_identity

    items = {'S1-B1': item('block', 'Retain the current revision.', 'Version')}
    sources = {'S1': source(items)}
    spec = {**page(['T1']), 'name': '版本要求', 'theme_directory': '设计', 'page_type': 'card'}
    reduced = {**mapped(items, {'T1': [link('S1-B1', 'evidence')]}), 'writing_contract': WRITE_CONTRACT}
    prior = {'name': spec['name'], 'page_id': 'prior', 'revision_id': 'old-revision', 'page_type': 'card',
             'path': '30_Cards/设计/版本要求.md', 'short_id': 'different-identity'}
    layout = {'contract': TYPED_LAYOUT, 'knowledge_plan': CONTRACT, 'themes': [], 'directories': [],
              'occupied': [prior['path']], 'existing': {}, 'registered_pages': {'prior': prior}}
    reduced['card_layout'] = layout
    planned, _, expected = pack(sources, reduced, [spec], 40_000)
    candidate, = expected.values()
    assert planned and candidate['card_path'] != prior['path']
    assert candidate['existing_page_hints'][0]['match'] == 'name_only'
    pid = spec_identity(reduced, spec, None)
    layout['registered_pages']['prior'] = {**prior, 'short_id': pid}
    layout['existing'][pid] = prior['path']
    planned, held, expected = pack(sources, reduced, [spec], 40_000)
    assert not planned and held[0]['reason'] == 'registered_page_requires_revision'
    assert expected[pid]['existing_page_hints'][0]['revision_id'] == 'old-revision'
    assert expected[pid]['existing_page_hints'][0]['match'] == 'registered_identity'


def test_primary_context_asset_keeps_whole_inspection_and_expression():
    items = {'S1-A1': item('asset', '| controlled form |', 'Records')}
    sources = {'S1': source(items)}
    topic = {'T1': {'relations': [link('S1-A1', 'context')]}}
    duty = obligations(sources, items, topic, {'S1-A1'})['S1-A1']
    assert duty['mapped_roles'] == ['context']
    assert duty['inspect'] and duty['express']


def test_numbered_table_navigation_requires_all_links_and_no_substantive_text():
    block = {'kind': 'block', 'required_assets': ['S1-A96', 'S1-A97'], 'text': (
        '#### 中国核电工程有限公司\n**工程设计计算书校审记录表 编号： 共2页 第1页**\n'
        '[查看表格](tables/first.md)\n<!-- table-id: first; source-page: 21; qa: pass -->\n'
        'CNPE-MSD-END-006-R03A（续）\n中国核电工程有限公司\n'
        '**工程设计计算书校审记录表 编号： 共2页 第2页**\n[查看表格](tables/second.md)')}
    assets = {'S1-A96': {'kind': 'asset', 'label': 'tables/first.md'},
              'S1-A97': {'kind': 'asset', 'label': 'tables/second.md'},
              'S1-A98': {'kind': 'asset', 'label': 'tables/list.md'}}
    checklist = {'kind': 'block', 'required_assets': ['S1-A98'], 'text': (
        '#### 设计文件校审清单\n**校审清单编号：JSQD-X-XX-XXX-XX-XX 共 页 第 页**\n'
        '[查看表格](tables/list.md)\n<!-- source-page: 24 -->')}
    assert table_navigation(block, assets) == ['S1-A96', 'S1-A97']
    assert table_navigation(checklist, assets) == ['S1-A98']
    assert not table_navigation(block, {'S1-A96': assets['S1-A96']})
    assert not table_navigation({**checklist, 'text': checklist['text'] + '\n应由专业负责人签署。'}, assets)


def test_writing_uses_inline_coverage_and_retains_inspection_in_scoped_retry():
    items = {
        'S1-B1': item('block', 'Must preserve the condition.', 'Conditions'),
        'S1-A2': item('asset', 'Full table', 'Records'),
        'S1-B3': item('block', 'Independent instructions.', 'Independent'),
    }
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-B1', 'condition'), link('S1-A2', 'asset')],
                             'T2': [link('S1-B3', 'evidence')]})
    reduced['writing_contract'] = WRITE_CONTRACT
    requests, held, _ = pack(sources, reduced, [page(['T1']), page(['T2'])], 40_000)
    row, = requests
    assert not held and set(row['view']['pages']) == {'p1', 'p2'}
    slot = next(key for key, view in row['view']['pages'].items() if 'S1-A2' in view['materials'])
    view = row['view']['pages'][slot]
    assert not view['qa_impact'] and view['qa_annotations'] and 'publication' not in view['qa_annotations'][0]
    assert 'release-review issue' not in row['system']
    answer = {'sections': [{'heading': 'Requirements', 'content': 'Required condition. [S1-B1]'}],
              'context': [], 'unresolved': [], 'inspected_assets': []}
    with pytest.raises(ValueError, match='required_expressions_missing=S1-A2; asset_inspections_missing=S1-A2') as error:
        check_fragment(row, slot, answer)
    retry = writing_retry(row, [slot], {slot: answer}, {slot: str(error.value)})
    assert retry['budget']['fits'] and set(retry['view']['pages']) == {slot}
    assert retry['view']['structural_retry']['slots'][slot]['previous_answer'] == answer
    retry = writing_retry(row, [slot], {slot: answer}, {slot: str(error.value)}, inspection_patch=True)
    assert retry['view']['structural_retry']['slots'][slot]['previous_answer'] == answer
    answer['sections'][0]['content'] += ' Table requirements. [S1-A2]'
    patch = writing_retry(row, [slot], {slot: answer}, {slot: 'missing inspection'}, inspection_patch=True)
    assert set(patch['view']['pages'][slot]['materials']) == {'S1-A2'}
    assert 'previous_answer' not in patch['view']['structural_retry']['slots'][slot]
    restored = restore_locked_draft(patch, slot, {'inspected_assets': ['S1-A2']}, answer)
    assert restored['sections'] == answer['sections'] and not answer['inspected_assets']
    assert check_fragment(row, slot, restored)['state'] == 'structurally_valid'
    with pytest.raises(ValueError, match='exactly the supplied'):
        restore_locked_draft(patch, slot, {'inspected_assets': ['S1-A2'], 'sections': []}, answer)
    with pytest.raises(ValueError, match='draft changed'):
        restore_locked_draft(patch, slot, {'inspected_assets': ['S1-A2']}, {**answer, 'sections': []})
    answer['inspected_assets'].append('S1-A2')
    assert check_fragment(row, slot, answer)['state'] == 'structurally_valid'


def test_output_only_split_preserves_complete_packet_and_condition_edges():
    items = {'S1-B1': item('block', 'Condition. ' + 'x' * 4200, 'Conditions'),
             'S1-B2': item('block', 'Exception. ' + 'y' * 2800, 'Exceptions'),
             'S1-A3': item('asset', '| Complete table |' + 'z' * 7000, 'Form')}
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-B1', 'condition', 'S1-A3'),
                                   link('S1-B2', 'exception', 'S1-A3'), link('S1-A3', 'asset')]})
    reduced['writing_contract'] = WRITE_CONTRACT
    requests, held, expected = pack(sources, reduced, [page(['T1'])], 40_000)
    assert not requests and held[0]['reason'] == 'atomic_evidence_over_budget'
    requests, gaps, split = pack(sources, reduced, [page(['T1'])], 40_000, split_output=True)
    assert not gaps and len(requests) == 2
    assert list(expected) == list(split)
    views = [row['view']['pages']['p1'] for row in requests]
    assert views[0]['materials'] == views[1]['materials']
    assert views[0]['topics'] == views[1]['topics']
    assert [v['writing_assignment']['primary'] for v in views] == [['S1-B1', 'S1-B2'], ['S1-A3']]
    assert all(v['obligations']['S1-A3']['inspect'] for v in views)
    assert all(row['budget']['fits'] for row in requests)
    requests, gaps, _ = pack(sources, reduced, [page(['T1'])], 10_000, split_output=True)
    assert not requests and gaps[0]['reason'] == 'atomic_evidence_over_budget'


def test_oversized_whole_asset_without_unresolved_mapping_is_serializable():
    items = {'S1-A1': item('asset', 'a' * 45_000, 'Records')}
    sources = {'S1': source(items)}
    reduced = mapped(items, {'T1': [link('S1-A1', 'context')]})
    requests, held, _ = pack(sources, reduced, [page(['T1'])], 40_000)
    assert not requests
    entry, = held
    assert entry['reason'] == 'atomic_evidence_over_budget'
    assert entry['unresolved'] == []
    assert json.loads(json.dumps(held)) == held


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


def test_appendix_form_wrapper_keeps_normative_text():
    block = item('block', '## 附录 1: ABC-END-006-R01A 设计校审记录卡\n\n'
                 '**设计校审记录卡 编号： 共2页 第1页**\n\n[表](tables/form.md)', 'one', ['S1-A2'])
    assert form_navigation(block, {'tables/form.md'})
    assert not form_navigation(block, {'tables/different.md'})
    assert not form_navigation({**block, 'text': block['text'] + '\n应核对密级。'}, {'tables/form.md'})


def test_exact_peer_coverage_requires_both_assets_in_same_section():
    items = {'S1-A1': item('asset', 'Revision', 'one'), 'S1-A2': item('asset', 'Approval', 'two')}
    data = mapped(items, {'T1': [link('S1-A1', 'asset', 'S1-A2'), link('S1-A2', 'asset')]})
    rows, _, expected = pack({'S1': source(items)}, data, [page(['T1'])], 40_000)
    row, = rows
    donor = row['view']['pages']['p1']
    target = {**donor, 'fragment_id': 'prior-held'}
    answer = {'sections': [{'heading': 'Revision and approval', 'content': 'Test [S1-A1] [S1-A2]',
                            'expressed': ['S1-A1', 'S1-A2']}],
              'context': [], 'unresolved': [], 'inspected_assets': ['S1-A1', 'S1-A2']}
    receipt = check_fragment(row, 'p1', answer)
    reuse = equivalent_coverage(target, donor, receipt)
    assert reuse['expressed'] == ['S1-A1', 'S1-A2']
    expected[donor['id']]['fragments'].insert(0, target['fragment_id'])
    assert build(expected, [receipt], [reuse])[donor['id']]['content'].count('Test [S1-A1]') == 1
    answer['sections'] = [{'heading': ref, 'content': f'Test [{ref}]', 'expressed': [ref]}
                          for ref in ('S1-A1', 'S1-A2')]
    assert equivalent_coverage(target, donor, check_fragment(row, 'p1', answer)) is None


def test_exact_continuation_keeps_ambiguous_spans_unresolved():
    data = {'items': {
        'S1-B1': {'section': '6.3', 'text': '### 6.3 Scope\n\nRequirements:',
                  'locator': {'path': 'document.md', 'span': {'start': 0, 'end': 30}}},
        'S1-B2': {'section': '6.3', 'locator': {'path': 'document.md', 'span': {'start': 0, 'end': 50}}},
        'S1-B3': {'section': '6.3.1', 'locator': {'path': 'document.md', 'span': {'start': 50, 'end': 80}}},
    }}
    assert exact_continuations(data, 'S1-B1', ['S1-B2', 'S1-B3'])['refs'] == ['S1-B2', 'S1-B3']
    data['items']['S1-B4'] = {'section': '6.3', 'locator': {'path': 'document.md', 'span': {'start': 50, 'end': 60}}}
    assert exact_continuations(data, 'S1-B1', ['S1-B2', 'S1-B3', 'S1-B4'])['status'] == 'unresolved'


def test_structured_missing_report_keeps_packet_and_source_scope_distinct():
    index = {'S1-B1': [], 'S1-B2': [{'page_id': 'peer'}], 'S1-B3': []}
    view = {'id': 'target', 'materials': {'S1-B1': {}}, 'material_locations_sha256': fingerprint(index)}
    result = verify_block({'material_locations': index}, view, ['S1-B1', 'S1-B2', 'S1-B3', 'S1-B4'])
    assert [row['status'] for row in result] == ['supplied', 'other_page', 'registered_unsupplied', 'unlocated']
