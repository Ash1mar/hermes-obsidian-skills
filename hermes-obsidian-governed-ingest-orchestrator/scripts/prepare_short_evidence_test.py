#!/usr/bin/env python3
"""Prepare or advance an isolated, offline short-evidence experiment."""
import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

from hermes_source_units import ContractError
from hermes_source_units.file_locks import exclusive_lock
from hermes_source_units.source_units import _json_bytes, _load_json, _write_atomic
from hermes_source_units.validation import fingerprint
from short_evidence_pass import (
    CONTRACT, SEED_RESERVE, load_sources, map_requests, mapping_groups, mapping_request,
    checked_answer, receipt, require, seed_requests,
)
from short_evidence_reduce import (
    build_sections, check_page, coordination_request, detail_request, expand_sections, pack_pages,
    page_plan, page_view, reduce_mappings, repair_requests,
)
import short_page_contract as pages_v2
import short_page_fragments as fragments_v2
import short_fragment_repair
import short_fragment_disposition


def inventory(sources):
    return {key: {
        **{k: v for k, v in source.items() if k not in ('items', 'outline')},
        'sections': len(source['outline']),
        'counts': dict(Counter(item['kind'] for item in source['items'].values())),
        'items': {
            item_id: {k: v for k, v in item.items() if k not in ('text', 'image')}
            for item_id, item in source['items'].items()
        },
    } for key, source in sources.items()}


def totals(rows):
    budgets = [row['budget'] for row in rows]
    return {
        'requests': len(rows), 'input_codepoints': sum(b['input_size'] for b in budgets),
        'max_request_codepoints': max((b['input_size'] for b in budgets), default=0),
        'reserved_codepoints': sum(b['input_reserve'] for b in budgets),
        'text_and_schema_utf8_bytes': sum(b['utf8_bytes'] for b in budgets),
        'image_count': sum(b['image_count'] for b in budgets),
        'image_bytes': sum(b['image_bytes'] for b in budgets),
        'estimated_output_tokens': sum(b['estimated_output_tokens'] for b in budgets),
        'output_token_caps': sum(row['output_cap'] for row in rows),
        'text_tokens': sum(b['text_tokens'] for b in budgets) if all(b['text_tokens'] is not None for b in budgets) else None,
    }


def structural_pages(sources):
    """Size real material by existing sections without proposing semantic pages."""
    result = []
    for key, source in sources.items():
        groups = {}
        for item_id, item in source['items'].items():
            groups.setdefault(item['section'], []).append(item_id)
        titles = {s['id']: s['title'] for s in source['outline']}
        for section, selected in groups.items():
            view, images = page_view(
                sources, {'name': titles.get(section, 'Standalone registered assets'),
                          'scope': 'Offline section-sized packing sample'},
                fingerprint([key, section]), {key: selected},
            )
            view['topics'] = {}
            result.append({'view': view, 'images': images, 'hold': []})
    return result


def preview(sources, seeds, groups):
    packets = [
        mapping_request(key, sources[key], ids, [], reserve=SEED_RESERVE)
        for key, packs in groups.items() for ids in packs
    ]
    result = {
        'stage': 'preview', 'seed_requests': seeds, 'mapping_templates': packets,
        'mapping_templates_require_seed_receipts': True,
        'seed_budget': totals(seeds), 'mapping_budget_before_seed_catalogue': totals(packets),
        'coverage': dict(Counter(item for row in packets for item in row['view']['assigned'])),
        'body_scenarios': {},
    }
    require(result['coverage'] == {item: 1 for source in sources.values() for item in source['items']},
            'incomplete offline mapping coverage')
    pages = structural_pages(sources)
    for limit in (40_000, 50_000):
        rows, held = pack_pages(pages, limit)
        result['body_scenarios'][str(limit)] = {
            'basis': 'one sizing slot per existing populated source section, not a semantic page plan',
            'sections': len(pages), 'requests': rows, 'held': held, 'budget': totals(rows),
        }
    return result


def collect(rows, saved, overlay=None):
    accepted, waiting = {}, []
    for row in rows:
        path = saved / f"{row['id']}.json"
        if overlay is not None and not path.is_file():
            path = overlay / path.name
        if path.is_file():
            record = _load_json(path)
            checked_answer(row, record)
            accepted[row['id']] = record
        else:
            waiting.append(row)
    return accepted, waiting


def advance(sources, seeds, groups, saved, limit, *, native_holds=None, page_contract=None, fragment_repair=None,
            fragment_exclusions=None, mapping_contract=None, mapping_overlay=None, preserved_pages=(),
            writing_contract=None, split_output=None, page_namespace=None, card_layout=None):
    from short_evidence_pass import MAP_CONTRACT, MAP_RECEIPT

    native_holds = native_holds or {}
    accepted, waiting = collect(seeds, saved)
    if waiting:
        if any(row['id'] in native_holds for row in waiting):
            return {'stage': 'topic_seed_held', 'held': [row['id'] for row in waiting], 'reused': list(accepted)}
        return {'stage': 'topic-seed', 'requests': waiting, 'reused': list(accepted)}
    maps = map_requests(sources, groups, seeds, accepted, mapping_contract)
    mapped, waiting = collect(maps, saved, mapping_overlay)
    map_holds = {row['id']: native_holds[row['id']] for row in waiting if row['id'] in native_holds}
    waiting = [row for row in waiting if row['id'] not in map_holds]
    if waiting:
        return {'stage': 'map', 'requests': waiting, 'reused': list(mapped)}
    repairs = repair_requests(sources, [row for row in maps if row['id'] in mapped], mapped,
                              MAP_CONTRACT if mapping_overlay is not None else mapping_contract)
    held_repairs = [row for row in repairs if not row['budget']['fits']]
    available_repairs = [row for row in repairs if row['budget']['fits'] and row['id'] not in native_holds]
    repaired, waiting = collect(available_repairs, saved)
    if waiting:
        return {'stage': 'local-map', 'requests': waiting, 'held': held_repairs, 'reused': list(repaired)}
    reduced = reduce_mappings(sources, maps, {**mapped, **repaired}, available_repairs, execution_holds=map_holds)
    versioned = {rid: {'issues': r['issues'], 'receipt_sha256': r['receipt_sha256'],
                      **({'native_replay': r['native_replay']} if 'native_replay' in r else {})}
                 for rid, r in {**mapped, **repaired}.items() if r['contract'] == MAP_RECEIPT}
    if versioned:
        reduced['mapping_receipts'] = versioned
    if mapping_overlay is not None:
        recovered = {item for item, d in reduced['dispositions'].items() if d['status'] == 'linked'
                     and d['request'] in versioned and 'native_replay' in versioned[d['request']]}
        affected = {tid: topic for tid, topic in reduced['topics'].items()
                    if any(link['item'] in recovered or recovered.intersection(link['applies_to'])
                           for link in topic['relations'])}
        reduced['mapping_recovery'] = {'recovered_items': sorted(recovered), 'affected_topics': sorted(affected),
                                       'preserved_pages': sorted(preserved_pages)}
        reduced['topics'] = {tid: topic for tid, topic in affected.items()
                             if fragments_v2.page_identity([topic['identity']], page_namespace) not in preserved_pages}
    if page_contract == pages_v2.CONTRACT:
        if writing_contract:
            reduced['writing_contract'] = writing_contract
        if card_layout:
            reduced['card_layout'] = card_layout
        return advance_fragments(sources, reduced, saved, limit, native_holds, held_repairs,
                                 fragment_repair, fragment_exclusions, split_output, page_namespace)
    coordination = coordination_request(sources, reduced)
    coordinated = None
    resolutions = {}
    if coordination is not None:
        if not coordination['budget']['fits']:
            return {'stage': 'coordination_held', 'held': [coordination], 'reduced': reduced}
        verified, waiting = collect([coordination], saved)
        if waiting:
            if coordination['id'] in native_holds:
                return {'stage': 'coordination_held', 'held': [coordination['id']], 'reduced': reduced}
            return {'stage': 'coordination', 'requests': waiting, 'reduced': reduced}
        coordinated = verified[coordination['id']]
        result = checked_answer(coordination, coordinated)
        local = [coordination_request(sources, reduced, issue) for issue in result['unresolved']]
        resolutions, waiting = collect([row for row in local if row['budget']['fits'] and row['id'] not in native_holds], saved)
        if waiting:
            return {'stage': 'local_coordination', 'requests': waiting, 'reduced': reduced,
                    'reused': list(resolutions), 'held': [row for row in local if not row['budget']['fits']]}
    pages, coordination_issues = page_plan(sources, reduced, coordinated, resolutions)
    planned, held = pack_pages(pages, limit)
    needing_detail = {row['page'] for row in held if row['reason'] == 'bounded_page_detail_required'}
    details = {}
    for page in pages:
        if page['view']['id'] in needing_detail:
            row = detail_request(page, limit)
            if row is not None and row['budget']['fits'] and row['id'] not in native_holds:
                details[row['id']] = (page, row)
    decided, waiting = collect([row for _, row in details.values()], saved)
    if waiting:
        return {'stage': 'local_page_detail', 'requests': waiting, 'held': held, 'reused': list(decided)}
    if decided:
        for key, record in decided.items():
            page, row = details[key]
            answer = checked_answer(row, record)
            if 'sections' in answer:
                page['sections'] = expand_sections(sources, reduced, page['view'], answer['sections'])
        planned, held = pack_pages(pages, limit)
    requests, checks = [], []
    for row in planned:
        missing = []
        for slot in row['view']['pages']:
            path = saved / f"{row['id']}-{slot}.json"
            if path.is_file():
                record = _load_json(path)
                require(check_page(row, slot, record['answer']) == record, 'changed page receipt')
                checks.append(record)
            else:
                missing.append(slot)
        if missing:
            if row['id'] in native_holds:
                held.extend({'page': row['view']['pages'][slot]['id'], 'slot': slot,
                             'reason': 'native_page_request_held', 'request': row['id']} for slot in missing)
            else:
                requests.append({**row, 'missing_slots': missing})
    return {'stage': 'writing' if requests else 'offline_build', 'requests': requests,
            'checks': checks, 'assembled_sections': build_sections(checks),
            'held': held, 'reduced': reduced, 'held_repairs': held_repairs,
            'coordination_issues': coordination_issues}


def advance_fragments(sources, reduced, saved, limit, native_holds, held_repairs, fragment_repair=None,
                      fragment_exclusions=None, split_output=None, page_namespace=None):
    common = {'page_contract': pages_v2.CONTRACT, 'reduced': reduced, 'held_repairs': held_repairs}
    topics = reduced['topics']
    coordination = pages_v2.coordination_request(sources, reduced)
    issues = []
    syntheses = []
    if coordination is None:
        specs = [{'name': topic['name'], 'scope': topic['scope'], 'topics': [tid], 'sections': [],
                  'reader_question': topic['scope'], 'grouping_reason': 'One mapped topic.',
                  'grouping_basis': 'single_topic'} for tid, topic in topics.items()]
    else:
        rid = coordination['id']
        batches = pages_v2.coordination_batches(sources, reduced, coordination)
        if batches == [coordination] and (not coordination['budget']['fits'] or rid in native_holds):
            return {**common, 'stage': 'coordination_held', 'held': [coordination]}
        eligible = [row for row in batches if row['budget']['fits'] and row['id'] not in native_holds]
        verified, waiting = collect(eligible, saved)
        if waiting:
            return {**common, 'stage': 'coordination', 'requests': waiting}
        answer = {'pages': [], 'unresolved': []}
        for row in batches:
            if row['id'] in verified:
                part = checked_answer(row, verified[row['id']])
                answer['pages'].extend(part['pages'])
                answer['unresolved'].extend(part['unresolved'])
                syntheses.extend(part.get('syntheses', []))
            else:
                issues.append({'topics': list(row['view']['topics']), 'request': row['id'], 'budget': row['budget'],
                               'reason': 'bounded coordination unavailable'})
        review = pages_v2.coordination_issues(answer, topics)
        reviewed = {tid for issue in review for tid in issue['topics']}
        specs = [page for page in answer['pages'] if not reviewed.intersection(page['topics'])]
        local = [(issue, pages_v2.coordination_request(sources, reduced, issue)) for issue in review]
        eligible = [row for _, row in local if row['budget']['fits'] and row['id'] not in native_holds]
        verified, waiting = collect(eligible, saved)
        if waiting:
            return {**common, 'stage': 'local_coordination', 'requests': waiting, 'reused': list(verified)}
        for issue, row in local:
            if row['id'] in verified:
                outcome = checked_answer(row, verified[row['id']])
                specs.extend(outcome['pages'])
                issues.extend(outcome['unresolved'])
            else:
                issues.append({**issue, 'request': row['id'], 'budget': row['budget']})
        if batches != [coordination]:
            reduced = {**reduced, 'bounded_coordination': True}
            common['reduced'] = reduced
    if syntheses:
        from short_knowledge_plan import syntheses as plan_syntheses
        shared, gaps = plan_syntheses(reduced, syntheses, specs, namespace=page_namespace,
                                     entry_id=lambda spec: fragments_v2.spec_identity(reduced, spec, page_namespace))
        specs.extend(shared)
        issues.extend(gaps)
    selected_split = split_output if isinstance(split_output, str) else None
    oversized = {} if selected_split else None
    planned, held, expected = fragments_v2.pack(sources, reduced, specs, limit, split_output=split_output is True,
                                                oversized=oversized, page_namespace=page_namespace)
    excluded = short_fragment_disposition.exclusions(planned, saved, fragment_exclusions)
    if excluded:
        common['fragment_exclusions'] = list(excluded.values())
    replaced, covered = set(), []
    if fragment_repair is not None:
        added, replaced, covered, isolated, expected, consumed = short_fragment_repair.apply(
            planned, expected, saved, fragment_repair, limit, sources=sources, excluded=excluded)
        planned.extend(added)
        held.extend(isolated)
        common['fragment_repair'] = {'contract': fragment_repair['contract'],
                                     'original_requests': consumed, 'replaced_or_isolated': sorted(replaced),
                                     'coverage_receipts': covered}
    if selected_split:
        require(selected_split in oversized, 'output split requires an exact oversized fragment')
        parts = fragments_v2.split_atomic_output(oversized[selected_split], limit)
        if parts:
            page_id = oversized[selected_split]['view']['id']
            members = expected[page_id]['fragments']
            index = members.index(selected_split)
            members[index:index + 1] = [part['view']['fragment_id'] for part in parts]
            held = [row for row in held if row['fragment_id'] != selected_split]
            planned.extend(pages_v2.body_request([part], limit) for part in parts)
            common['output_split'] = {'original_fragment': selected_split, 'parts': members[index:index + len(parts)]}
    checks, requests = [], []
    for row in planned:
        rid = row['id']
        missing = []
        for slot, view in row['view']['pages'].items():
            if view['fragment_id'] in replaced:
                continue
            if view['fragment_id'] in excluded:
                record = excluded[view['fragment_id']]
                held.append({**record, 'page': record['page_id'], 'request': rid, 'slot': slot})
                continue
            path = saved / f'{rid}-{slot}.json'
            if path.is_file():
                record = _load_json(path)
                require(pages_v2.check_fragment(row, slot, record['answer']) == record, 'changed fragment receipt')
                checks.append(record)
            elif rid in native_holds:
                held.append({'page': view['id'], 'fragment_id': view['fragment_id'], 'slot': slot,
                             'reason': 'native_fragment_request_held', 'request': rid})
            else:
                missing.append(slot)
        if missing:
            requests.append({**row, 'missing_slots': missing})
    if fragment_repair and fragment_repair.get('section_delivery'):
        require(bool(reduced.get('card_layout')), 'section delivery requires readable short cards')
        expected = fragments_v2.independent_sections(expected, checks, covered, specs,
            {**reduced, 'page_namespace': page_namespace}, planned, held)
    return {
        **common, 'stage': 'writing' if requests else 'offline_build', 'requests': requests, 'checks': checks,
        'expected_pages': expected, 'assembled_sections': fragments_v2.build(
            expected, checks, covered, excluded_receipts={row['receipt_sha256'] for row in excluded.values()}),
        'held': held, 'coordination_issues': issues,
    }


def save(path, value):
    with exclusive_lock(path.with_suffix(path.suffix + '.lock'), timeout=5, marker=False):
        if path.is_file():
            require(_load_json(path) == value, f'existing output has a different identity: {path}')
        else:
            _write_atomic(path, _json_bytes(value))


def seal(plan, request_id, response, saved, *, call=None, drafts=None):
    """Accept already captured replies one slot at a time for an isolated test."""
    row = next((r for r in plan['requests'] if r['id'] == request_id), None)
    require(row is not None, 'request is not pending in this saved plan')
    body = {k: row[k] for k in ('contract', 'phase', 'messages', 'system', 'schema', 'transport_schema', 'output_cap')}
    require(fingerprint(body) == row['id'], 'request wire was changed')
    if row['phase'] != 'short-write':
        save(saved / f'{request_id}.json', receipt(row, response))
        return {'sealed': ['r'], 'held': {}}
    sealed, held, content_held = [], {}, {}
    require(isinstance(response, dict) and set(response) == {'results'} and isinstance(response['results'], dict),
            'page response requires result slots')
    results = response['results']
    require(set(results) <= row['view']['pages'].keys(), 'unknown page result slot')
    for slot in row['missing_slots']:
        try:
            require(slot in results, 'missing page result')
            validator = pages_v2.check_fragment if row['view'].get('page_contract') == pages_v2.CONTRACT else check_page
            answer = results[slot] if call is None else pages_v2.restore_locked_draft(call, slot, results[slot], drafts.get(slot))
            record = validator(row, slot, answer)
            save(saved / f'{request_id}-{slot}.json', record)
            sealed.append(slot)
            if record.get('state') == 'held':
                content_held[slot] = record.get('diagnostic')
        except (ValueError, ContractError) as error:
            held[slot] = str(error)
    return {'sealed': sealed, 'held': held, **({'content_held': content_held} if content_held else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True, help='JSON array of exact approved source pins')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipts', type=Path)
    parser.add_argument('--body-limit', type=int, choices=(40_000, 50_000), default=40_000)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--request-id')
    parser.add_argument('--response', type=Path)
    args = parser.parse_args()
    vault = args.vault.resolve(strict=True)
    try:
        for target in (args.output, args.receipts):
            require(target is None or not target.resolve().is_relative_to(vault), 'output must be outside the source Vault')
        sources = load_sources(vault, _load_json(args.sources))
        require(sources, 'pin at least one source')
        pins = inventory(sources)
        seeds, groups = seed_requests(sources), mapping_groups(sources)
        if args.response:
            require(args.plan and args.request_id and args.receipts, 'response capture needs a saved plan, ID and receipt directory')
            plan = _load_json(args.plan)
            current = advance(sources, seeds, groups, args.receipts, args.body_limit)
            current_rows = {r['id']: r for r in current.get('requests', [])}
            require(plan['sources'] == pins and args.request_id in current_rows,
                    'request no longer belongs to the current exact inputs')
            result = seal({'requests': list(current_rows.values())}, args.request_id, _load_json(args.response), args.receipts)
        elif args.receipts:
            result = advance(sources, seeds, groups, args.receipts, args.body_limit)
        else:
            result = preview(sources, seeds, groups)
        save(args.output, {'contract': CONTRACT, 'sources': pins, 'model_calls': 0, **result})
    except (ValueError, ContractError, KeyError, OSError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
