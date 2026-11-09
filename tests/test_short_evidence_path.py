"""Exercise the new offline protocol without a Vault, native task or model."""
from pathlib import Path
import sys

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

from prepare_short_evidence_test import advance, seal
from short_evidence_pass import (
    checked_answer, map_requests, mapping_groups, mapping_request, receipt, seed_requests,
)
from short_evidence_reduce import (
    build_sections, check_page, coordination_request, detail_request, expand_sections, pack_pages,
    page_plan, reduce_mappings,
)


def source(key, size=24, qa=False):
    items = {}
    for iid, kind, text, required in (
        (f'{key}-B1', 'block', 'x' * size, [f'{key}-A2']),
        (f'{key}-A2', 'asset', '| full table |', []),
    ):
        items[iid] = {'kind': kind, 'text': text, 'required_assets': required,
                      'heading': ['Scope'], 'locator': {'path': 'tables/test.md'}}
    return {
        'name': key, 'identity': {'resource_id': key}, 'unit_set_id': key,
        'document_sha256': key, 'source_sha256': key, 'bundle_sha256': key, 'registry_revision': 1,
        'outline': [{'id': 'section-1', 'parent': None, 'title': 'Scope'}],
        'qa': {'review_required': ['table'] if qa else []}, 'items': items,
    }


def result(**fields):
    return {'results': {'r': fields}}


def linked(iid, topic='t1', applies_to=()):
    return {'item': iid, 'status': 'linked', 'reason': '', 'links': [{
        'topic': topic, 'role': 'asset' if '-A' in iid else 'evidence',
        'applies_to': list(applies_to), 'note': '',
    }]}


def save_reply(plan, row, answer, directory):
    return seal(plan, row['id'], answer, directory)


def test_pipeline_reuses_completed_slots_and_bounds_repairs(tmp_path):
    sources = {'S1': source('S1'), 'S2': source('S2', qa=True)}
    seeds, groups = seed_requests(sources), mapping_groups(sources)
    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    seed = result(topics=[{'id': 'seed', 'name': 'Same title', 'scope': 'Scoped test'}])
    for row in plan['requests']:
        save_reply(plan, row, seed, tmp_path)
    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    assert plan['stage'] == 'map'
    for row in plan['requests']:
        key = row['view']['source']
        block = linked(f'{key}-B1')
        if key == 'S1':
            block = {**block, 'status': 'pending', 'reason': 'condition ambiguous', 'links': []}
        save_reply(plan, row, result(topics=[], dispositions=[block, linked(f'{key}-A2')]), tmp_path)
    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    assert plan['stage'] == 'local-map'
    assert len(plan['requests']) == 1
    row = plan['requests'][0]
    assert row['view']['materials']['S1-A2']['text'] == '| full table |'
    save_reply(plan, row, result(topics=[], dispositions=[linked('S1-B1')]), tmp_path)

    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    assert plan['stage'] == 'coordination'
    row = plan['requests'][0]
    assert len(row['view']['topics']) == 2
    pages = [{'name': topic['source'], 'scope': 'Different source scope', 'topics': [tid], 'sections': []}
             for tid, topic in row['view']['topics'].items()]
    save_reply(plan, row, result(pages=pages[:1], unresolved=[
        {'topics': pages[1]['topics'], 'reason': 'require original table to verify the boundary'},
    ]), tmp_path)
    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    assert plan['stage'] == 'local_coordination'
    row = plan['requests'][0]
    assert len(row['view']['topics']) == 1 and len(row['view']['flagged_evidence']) == 2
    save_reply(plan, row, result(pages=pages[1:], unresolved=[]), tmp_path)

    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    row, = plan['requests']
    assert plan['stage'] == 'writing' and row['missing_slots'] == ['p1', 'p2']
    answers = {}
    for slot, page in row['view']['pages'].items():
        refs = page['required_citations']
        answers[slot] = {'content': ' '.join(f'[{iid}]' for iid in refs), 'citations': refs,
                         'inspected_assets': page['required_assets']}
    partial = save_reply(plan, row, {'results': {'p1': answers['p1']}}, tmp_path)
    assert partial['sealed'] == ['p1'] and 'p2' in partial['held']
    plan = advance(sources, seeds, groups, tmp_path, 40_000)
    assert plan['requests'][0]['missing_slots'] == ['p2']
    save_reply(plan, row, {'results': answers}, tmp_path)
    done = advance(sources, seeds, groups, tmp_path, 40_000)
    assert done['stage'] == 'offline_build'
    assert {check['state'] for check in done['checks']} == {'qa_held', 'structurally_valid'}
    assert not any(check['publication_eligible'] for check in done['checks'])


def test_rejects_unaccounted_material_and_stale_receipts():
    s = source('S1')
    row = mapping_request('S1', s, list(s['items']), [{'id': 't1', 'name': 'Topic', 'scope': 'Scope'}])
    with pytest.raises(ValueError, match='exactly one disposition'):
        receipt(row, result(topics=[], dispositions=[linked('S1-B1')]))
    with pytest.raises(ValueError, match='out-of-packet'):
        receipt(row, result(topics=[], dispositions=[linked('S1-B1', applies_to=['S2-B1']), linked('S1-A2')]))
    answer = result(topics=[], dispositions=[linked('S1-B1'), linked('S1-A2')])
    saved = receipt(row, answer)
    changed = mapping_request('S1', {**s, 'registry_revision': 2}, list(s['items']), row['view']['seed_topics'])
    with pytest.raises(ValueError, match='stale'):
        checked_answer(changed, saved)


def test_oversize_body_keeps_one_page_and_complete_sections():
    sources = {key: source(key, 9000) for key in ('S1', 'S2')}
    seeds, groups = seed_requests(sources), mapping_groups(sources)
    responses = {row['id']: receipt(row, result(topics=[{'id': 'seed', 'name': 'Topic', 'scope': 'Scope'}]))
                 for row in seeds}
    maps = map_requests(sources, groups, seeds, responses)
    mapped = {row['id']: receipt(row, result(topics=[], dispositions=[linked(i) for i in row['view']['assigned']]))
              for row in maps}
    reduced = reduce_mappings(sources, maps, mapped)
    call = coordination_request(sources, reduced)
    topics = list(reduced['topics'])
    page = {'name': 'Unified', 'scope': 'Two versions', 'topics': topics,
            'sections': [{'name': tid, 'topics': [tid]} for tid in topics]}
    coordinated = receipt(call, result(pages=[page], unresolved=[]))
    pages, issues = page_plan(sources, reduced, coordinated)
    packed, held = pack_pages(pages, 40_000)
    assert not issues and not held and len(packed) == 2
    pages[0]['sections'] = []
    assert pack_pages(pages, 40_000)[1][0]['reason'] == 'bounded_page_detail_required'
    local = detail_request(pages[0], 40_000)
    assert local['budget']['fits']
    with pytest.raises(ValueError, match='partition'):
        receipt(local, result(sections=page['sections'][:1]))
    decision = receipt(local, result(sections=page['sections']))
    sections = checked_answer(local, decision)['sections']
    pages[0]['sections'] = expand_sections(sources, reduced, pages[0]['view'], sections)
    assert pack_pages(pages, 40_000)[0] == packed
    checks = []
    for row in packed:
        view = row['view']['pages']['p1']
        refs = view['required_citations']
        checks.append(check_page(row, 'p1', {'content': ' '.join(f'[{ref}]' for ref in refs),
                                              'citations': refs, 'inspected_assets': view['required_assets']}))
    assert not build_sections(checks[:1])
    body, = build_sections(checks).values()
    assert '## T1' in body['content'] and '## T2' in body['content']
