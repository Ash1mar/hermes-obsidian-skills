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


def collect(rows, saved):
    accepted, waiting = {}, []
    for row in rows:
        path = saved / f"{row['id']}.json"
        if path.is_file():
            record = _load_json(path)
            checked_answer(row, record)
            accepted[row['id']] = record
        else:
            waiting.append(row)
    return accepted, waiting


def advance(sources, seeds, groups, saved, limit, *, native_holds=None):
    native_holds = native_holds or {}
    accepted, waiting = collect(seeds, saved)
    if waiting:
        if any(row['id'] in native_holds for row in waiting):
            return {'stage': 'topic_seed_held', 'held': [row['id'] for row in waiting], 'reused': list(accepted)}
        return {'stage': 'topic-seed', 'requests': waiting, 'reused': list(accepted)}
    maps = map_requests(sources, groups, seeds, accepted)
    mapped, waiting = collect(maps, saved)
    map_holds = {row['id']: native_holds[row['id']] for row in waiting if row['id'] in native_holds}
    waiting = [row for row in waiting if row['id'] not in map_holds]
    if waiting:
        return {'stage': 'map', 'requests': waiting, 'reused': list(mapped)}
    repairs = repair_requests(sources, [row for row in maps if row['id'] in mapped], mapped)
    held_repairs = [row for row in repairs if not row['budget']['fits']]
    available_repairs = [row for row in repairs if row['budget']['fits'] and row['id'] not in native_holds]
    repaired, waiting = collect(available_repairs, saved)
    if waiting:
        return {'stage': 'local-map', 'requests': waiting, 'held': held_repairs, 'reused': list(repaired)}
    reduced = reduce_mappings(sources, maps, {**mapped, **repaired}, available_repairs, execution_holds=map_holds)
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


def save(path, value):
    with exclusive_lock(path.with_suffix(path.suffix + '.lock'), timeout=5, marker=False):
        if path.is_file():
            require(_load_json(path) == value, f'existing output has a different identity: {path}')
        else:
            _write_atomic(path, _json_bytes(value))


def seal(plan, request_id, response, saved):
    """Accept already captured replies one slot at a time for an isolated test."""
    row = next((r for r in plan['requests'] if r['id'] == request_id), None)
    require(row is not None, 'request is not pending in this saved plan')
    body = {k: row[k] for k in ('contract', 'phase', 'messages', 'system', 'schema', 'transport_schema', 'output_cap')}
    require(fingerprint(body) == row['id'], 'request wire was changed')
    if row['phase'] != 'short-write':
        save(saved / f'{request_id}.json', receipt(row, response))
        return {'sealed': ['r'], 'held': {}}
    sealed, held = [], {}
    require(isinstance(response, dict) and set(response) == {'results'} and isinstance(response['results'], dict),
            'page response requires result slots')
    results = response['results']
    require(set(results) <= row['view']['pages'].keys(), 'unknown page result slot')
    for slot in row['missing_slots']:
        try:
            require(slot in results, 'missing page result')
            record = check_page(row, slot, results[slot])
            save(saved / f'{request_id}-{slot}.json', record)
            sealed.append(slot)
        except (ValueError, ContractError) as error:
            held[slot] = str(error)
    return {'sealed': sealed, 'held': held}


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
