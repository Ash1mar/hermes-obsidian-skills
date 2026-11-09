"""Receipt exclusions and exact batch material locations for short writing."""
import re

from hermes_source_units.validation import fingerprint
from short_evidence_pass import require

CONTRACT = 'short-fragment-exclusions/v1'


def exclusions(planned, saved, manifest):
    from short_fragment_repair import saved_fragment

    if manifest is None:
        return {}
    require(set(manifest) == {'contract', 'evidence', 'excluded'}
            and manifest['contract'] == CONTRACT, 'invalid fragment exclusion record')
    targets = {view['fragment_id']: (row, slot, view)
               for row in planned for slot, view in row['view']['pages'].items()}
    result = {}
    for item in manifest['excluded']:
        require(set(item) == {'fragment_id', 'receipt_sha256', 'page_id', 'reason'}
                and item['reason'].strip(), 'invalid exclusion binding')
        fid = item['fragment_id']
        require(fid in targets and fid not in result, 'unknown or duplicate excluded fragment')
        row, slot, view = targets[fid]
        receipt = saved_fragment(saved, row, slot)
        require(receipt and receipt['state'] == 'structurally_valid' and view['id'] == item['page_id']
                and receipt['receipt_sha256'] == item['receipt_sha256'], 'excluded receipt or page changed')
        result[fid] = {**item, 'category': 'unresolved', 'evidence': manifest['evidence']}
    return result


def locations(sources, planned, excluded):
    index = {item: [] for src in sources.values() for item in src['items']}
    withheld = {entry['page_id'] for entry in excluded.values()}
    for row in planned:
        for view in row['view']['pages'].values():
            entry = {'page_id': view['id'], 'name': view['name'],
                     'state': 'excluded_page' if view['id'] in withheld else 'planned_page'}
            for item in view['materials']:
                if entry not in index[item]:
                    index[item].append(entry)
    return index


def exact_continuations(source, opener, assigned):
    """Resolve an explicit section opener by containing or adjacent source spans."""
    item = source['items'][opener]
    section = item.get('section')
    text = item.get('text') or ''
    if (not section or not text.rstrip().endswith((':', '：'))
            or not re.match(r'^#{1,6}\s+' + re.escape(section) + r'(?:\s|$)', text.lstrip())):
        return None
    locator = item['locator']
    span = locator.get('span')
    if span is None:
        return None
    start, end = span['start'], span['end']
    file = locator['path']
    candidates = {}
    for key in assigned:
        sibling = source['items'].get(key)
        if key == opener or not sibling or sibling.get('section') != section:
            continue
        loc = sibling['locator']
        if loc.get('path') == file and 'span' in loc:
            candidates[key] = (loc['span']['start'], loc['span']['end'])
    resolved = []
    while True:
        # A same-start superset can expand a heading-only unit; later text
        # must begin exactly at the verified boundary.
        choices = {key: (a, b) for key, (a, b) in candidates.items()
                   if b > end and (a == end or (not resolved and a == start))}
        if not choices:
            break
        if len(choices) != 1:
            return {'section': section, 'status': 'unresolved', 'candidate_refs': sorted(choices)}
        key, (_, end) = next(iter(choices.items()))
        resolved.append(key)
    if not resolved:
        return {'section': section, 'status': 'unresolved', 'candidate_refs': []}
    return {'section': section, 'status': 'resolved', 'refs': resolved}


def context_for(view, sources, index):
    relations = []
    for item in view['materials']:
        key = item.split('-', 1)[0]
        other = {ref for ref, pages in index.items() if ref.startswith(key + '-')
                 and any(page['page_id'] != view['id'] for page in pages)}
        relation = exact_continuations(sources[key], item, other)
        if relation is None:
            continue
        relations.append({'opener': item, **relation, 'destinations': {
            ref: [page for page in index[ref] if page['page_id'] != view['id']]
            for ref in relation.get('refs', [])
        }})
    return relations


def verify_block(row, view, refs):
    index = row['material_locations']
    require(view['material_locations_sha256'] == fingerprint(index), 'material location inventory changed')
    result = []
    for ref in refs:
        elsewhere = [page for page in index.get(ref, []) if page['page_id'] != view['id']]
        status = ('supplied' if ref in view['materials'] else 'other_page' if elsewhere else
                  'registered_unsupplied' if ref in index else 'unlocated')
        result.append({'ref': ref, 'status': status, 'destinations': elsewhere})
    return result


def delivery_partition(plan):
    """Use the formal page dependency partition on the exact short Build result."""
    from hermes_source_units.delivery_scope import dependency_partition
    from short_page_fragments import build

    excluded = {row['receipt_sha256'] for row in plan.get('fragment_exclusions', [])}
    covered = plan.get('fragment_repair', {}).get('coverage_receipts', [])
    require(all(row['receipt_sha256'] == fingerprint({k: v for k, v in row.items() if k != 'receipt_sha256'})
                for row in plan['checks']), 'fragment receipt fingerprint changed')
    pages = build(plan['expected_pages'], plan['checks'], covered, excluded_receipts=excluded)
    require(pages == plan['assembled_sections'], 'short Build pages differ from the reviewed fragments')
    holds = {}
    for row in plan['held']:
        holds.setdefault(row['page'], []).append(row)
    candidates, reasons = [], {}
    for page_id, spec in plan['expected_pages'].items():
        body = pages.get(page_id)
        candidates.append({'page_id': page_id, 'path': f'20_Notes/ShortEvidence/{page_id}.md',
                           'identity': {'canonical_name': spec['name']}, 'content': body['content'] if body else ''})
        if not body:
            reasons[page_id] = {'category': 'unresolved', 'reason': 'missing accepted fragment coverage',
                                'evidence': holds.get(page_id, []),
                                'reopen_condition': 'verify the affected fragment under its bound contract'}
    return dependency_partition(candidates, reasons)
