"""Plan and check isolated pages directly from verified evidence mappings."""
import re

from hermes_source_units.semantic_submission import closed
from hermes_source_units.validation import _check_shape, fingerprint
from short_evidence_pass import (
    CONTRACT, NAME, NOTE, PASS_LIMIT, REF, USABLE_OUTPUT, array, checked_answer,
    evidence, mapping_request, request, require, source_revision,
)

SECTION = closed({'name': NAME, 'topics': array(REF)})
PAGE_SPEC = closed({'name': NAME, 'scope': {**NOTE, 'minLength': 1}, 'topics': array(REF),
                    'sections': array(SECTION)})
ISSUE = closed({'topics': array(REF), 'reason': {**NOTE, 'minLength': 1}})
COORDINATION = closed({'pages': array(PAGE_SPEC), 'unresolved': array(ISSUE)})
DETAIL = {'anyOf': [closed({'sections': array(SECTION)}),
                    closed({'blocked_reason': {**NOTE, 'minLength': 1}})]}
PAGE_RESULT = {'anyOf': [
    closed({'content': {'type': 'string', 'minLength': 1}, 'citations': array(REF),
            'inspected_assets': array(REF)}),
    closed({'blocked_reason': {**NOTE, 'minLength': 1}}),
]}
RULES = {
    'short-coordinate': (
        'Coordinate theme identity and page boundaries, including cross-source overlap, conflicts and '
        'different versions or scopes. Use the provided original evidence for flagged issues. Similar '
        'titles alone do not establish equivalence. Assign every topic ID exactly once to a page '
        'or an unresolved issue. Use unresolved for genuine ambiguity requiring more originals. Preserve '
        'scopes and conflicts in the page scope. Only propose sections when the topics form distinct '
        'coherent sections inside the same page. Sections must partition the page topics, or return an '
        'empty list. Return no page text or file paths.'
    ),
    'short-write': (
        'Generate each page slot independently in Chinese using only that slot’s complete source materials. '
        'Use the evidence relations to preserve applicability, conditions, exceptions, definitions, '
        'conflicting versions and necessary context. Preserve numerical limits, AND/OR and QA caveats. '
        'Inspect each required whole asset. Inline every required citation using [S1-B1] style IDs, '
        'without Markdown link targets, and list the used IDs in citations. List inspected_assets '
        'separately. Missing essential support or unreadable required assets require blocked_reason. '
        'For a page_section, return only that named section, without a heading for the whole page. '
        'Do not infer unsupported content. This is the only semantic page-generation step.'
    ),
    'short-detail': (
        'This one page exceeds its writing budget. Propose coherent independently writable sections '
        'inside the same logical page. Partition all topic IDs exactly once. Preserve every source '
        'scope, condition and exception. Do not propose other pages or remove evidence. '
        'If the topics cannot be separated safely, return blocked_reason.'
    ),
}


def repair_requests(sources, mapping_rows, receipts):
    """At most one bounded follow-up for an explicitly pending item."""
    rows = []
    for row in mapping_rows:
        response = checked_answer(row, receipts[row['id']])
        view = row['view']
        key = view['source']
        source = sources[key]
        for disposition in response['dispositions']:
            if disposition['status'] != 'pending':
                continue
            target = disposition['item']
            order = list(source['items'])
            index = order.index(target)
            context = [item for item in order[max(0, index - 1):index + 2] if item != target]
            candidate = mapping_request(
                key, source, [target], view['seed_topics'], context=context,
                issue={'item': target, 'reason': disposition['reason'], 'parent_request': row['id']},
            )
            rows.append(candidate)
    return rows


def reduce_mappings(sources, mapping_rows, receipts, repairs=()):
    topics, dispositions, revised = {}, {}, {}
    for row in repairs:
        view = row['view']
        answer = checked_answer(row, receipts[row['id']])
        require(view['issue']['item'] not in revised, 'duplicate local repair')
        revised[view['issue']['item']] = (row, answer)
    all_rows = [(row, checked_answer(row, receipts[row['id']])) for row in mapping_rows]
    used_repairs = set()
    for row, answer in [*all_rows, *revised.values()]:
        view = row['view']
        key = view['source']
        issue = view.get('issue')
        local = {}
        for topic in [*view['seed_topics'], *answer['topics']]:
            identity = fingerprint([key, 'seed' if topic in view['seed_topics'] else row['id'], topic['id']])
            local[topic['id']] = identity
            topics.setdefault(identity, {'source': key, 'name': topic['name'], 'scope': topic['scope'], 'relations': []})
        for disposition in answer['dispositions']:
            item = disposition['item']
            if issue:
                require(item == issue['item'] and dispositions[item]['status'] == 'pending'
                        and dispositions[item]['request'] == issue['parent_request']
                        and dispositions[item]['reason'] == issue['reason'], 'repair no longer matches its pending item')
                used_repairs.add(item)
            else:
                require(item not in dispositions, 'item assigned to several primary mapping requests')
            if item in revised and not issue:
                require(disposition['status'] == 'pending', 'local repair requires an explicit pending item')
            dispositions[item] = {
                'status': disposition['status'], 'reason': disposition['reason'], 'request': row['id'],
                'topics': sorted({local[link['topic']] for link in disposition['links']}),
            }
            for link in disposition['links']:
                identity = local[link['topic']]
                relation = {'item': item, **{k: v for k, v in link.items() if k != 'topic'}}
                if relation not in topics[identity]['relations']:
                    topics[identity]['relations'].append(relation)
    require(used_repairs == revised.keys(), 'unapplied local repair')
    require(dispositions.keys() == {item for source in sources.values() for item in source['items']},
            'material coverage differs from the pinned UnitSets')
    active = {}
    for identity, topic in topics.items():
        if topic['relations']:
            active[f'T{len(active) + 1}'] = {**topic, 'identity': identity}
    return {
        'contract': CONTRACT, 'topics': active, 'dispositions': dispositions,
        'unresolved': [item for item, row in dispositions.items() if row['status'] == 'pending'],
    }


def coordination_request(sources, reduced, issue=None):
    topics = reduced['topics'] if issue is None else {tid: reduced['topics'][tid] for tid in issue['topics']}
    if issue is None and len(topics) < 2:
        return None
    material, images = {}, []
    for key, source in sources.items():
        contested = dict.fromkeys(
            item for topic in topics.values() if topic['source'] == key
            for link in topic['relations'] if issue is not None or link['role'] in ('conflict', 'contrary')
            for item in (link['item'], *link['applies_to'])
        )
        items, pictures = evidence(source, contested)
        material.update(items)
        images.extend(pictures)
    catalogue = {
        tid: {**{k: row[k] for k in ('source', 'name', 'scope')},
              'relation_count': len(row['relations']),
              'issues': [link for link in row['relations'] if link['role'] in ('conflict', 'contrary')]}
        for tid, row in topics.items()
    }
    view = {
        'revisions': {k: source_revision(v) for k, v in sources.items()},
        'map_sha256': fingerprint(reduced), 'topics': catalogue, 'flagged_evidence': material,
    }
    if issue is not None:
        view['issue'] = issue
    else:
        view['unresolved_materials'] = {item: reduced['dispositions'][item]['reason'] for item in reduced['unresolved']}
    return request('short-coordinate', view, COORDINATION, 768 + 96 * len(topics), PASS_LIMIT, images)


def check_coordination(view, answer):
    topics = view['topics']
    entries = [*answer['pages'], *answer['unresolved']]
    require(all(entry['topics'] for entry in entries), 'empty coordinated topic group')
    members = [tid for entry in entries for tid in entry['topics']]
    require(len(members) == len(topics) and set(members) == topics.keys(),
            'coordination must account for each mapped topic exactly once')
    for page in answer['pages']:
        require(page['name'].strip() and page['scope'].strip(), 'empty page identity or scope')
        sections = page['sections']
        if sections:
            check_sections(page['topics'], sections)
    require(all(issue['reason'].strip() for issue in answer['unresolved']), 'empty coordination issue')


def page_plan(sources, reduced, coordinated=None, resolutions=None):
    topics = reduced['topics']
    if coordinated is None:
        require(len(topics) <= 1, 'several topics require a coordination result')
        specs = [{'name': t['name'], 'scope': t['scope'], 'topics': [key], 'sections': []}
                 for key, t in topics.items()]
        issues = []
    else:
        row = coordination_request(sources, reduced)
        require(row is not None, 'no coordination request is required')
        answer = checked_answer(row, coordinated)
        specs, issues = list(answer['pages']), []
        for issue in answer['unresolved']:
            local = coordination_request(sources, reduced, issue)
            if resolutions is not None and local['id'] in resolutions:
                decided = checked_answer(local, resolutions[local['id']])
                specs.extend(decided['pages'])
                issues.extend(decided['unresolved'])
            else:
                issues.append(issue)
    result = []
    for page in specs:
        page_id = fingerprint(sorted(topics[tid]['identity'] for tid in page['topics']))
        planned = expand_page(sources, reduced, page, page_id)
        planned['sections'] = expand_sections(sources, reduced, planned['view'], page['sections'])
        result.append(planned)
    return result, issues


def expand_page(sources, reduced, page, page_id):
    topics = {tid: reduced['topics'][tid] for tid in page['topics']}
    selection = {}
    for topic in topics.values():
        items = selection.setdefault(topic['source'], {})
        for link in topic['relations']:
            for item in (link['item'], *link['applies_to']):
                items[item] = None
    view, images = page_view(sources, page, page_id, selection)
    view['topics'] = topics
    unresolved = [item for item in view['required_citations']
                  if reduced['dispositions'][item]['status'] != 'linked']
    return {'view': view, 'images': images, 'hold': unresolved}


def check_sections(topics, sections):
    members = [tid for section in sections for tid in section['topics']]
    require(len(members) == len(topics) and set(members) == set(topics)
            and all(section['topics'] and section['name'].strip() for section in sections),
            'sections must partition one page without losing topics')


def expand_sections(sources, reduced, view, sections):
    parts = []
    for number, section in enumerate(sections, 1):
        part = expand_page(sources, reduced, {**view, 'topics': section['topics']}, view['id'])
        part['view']['page_section'] = {'number': number, 'total': len(sections), 'name': section['name']}
        parts.append(part)
    return parts


def detail_request(page, limit):
    view = page['view']
    if len(view['topics']) < 2:
        return None
    return request('short-detail', {
        'page': view, 'writing_input_cap': limit, 'writing_output_target': USABLE_OUTPUT,
        'reason': 'the full page and its current sections cannot fit the writing request budget',
    }, DETAIL, 768 + 96 * len(view['topics']), PASS_LIMIT, page['images'])


def page_view(sources, page, page_id, selection):
    """Expand exact whole sources for each page, including transitive assets and QA."""
    material, images, qa, revisions = {}, [], {}, {}
    for key, item_ids in selection.items():
        source = sources[key]
        items, pictures = evidence(source, item_ids)
        material.update(items)
        images.extend(pictures)
        qa[key] = source['qa']
        revisions[key] = source_revision(source)
    return {
        'id': page_id, 'name': page['name'], 'scope': page['scope'],
        'path': f'20_Notes/ShortEvidence/{page_id}.md', 'source_revisions': revisions,
        'qa': qa, 'materials': material, 'required_citations': list(material),
        'required_assets': [key for key, item in material.items() if item['kind'] == 'asset'],
    }, images


def body_request(group, limit):
    require(1 <= len(group) <= 2, 'body request needs one or two independent slots')
    require(len(group) == 1 or not any(p['images'] for p in group), 'image pages require a single slot')
    slots = {f'p{i}': p['view'] for i, p in enumerate(group, 1)}
    images = [image for p in group for image in p['images']]
    output = sum(900 + sum(len(item['text'] or '') for item in p['materials'].values()) // 2
                 + 20 * len(p['materials']) for p in slots.values())
    return request('short-write', {'pages': slots}, dict.fromkeys(slots, PAGE_RESULT),
                   output, limit, images, slots=True)


def pack_pages(pages, limit):
    """Look for a fitting peer without changing the knowledge-page boundaries."""
    held, pending = [], []
    for page in pages:
        row = body_request([page], limit)
        if not page['hold'] and row['budget']['fits']:
            pending.append((page, row))
            continue
        sections = [(part, body_request([part], limit)) for part in page.get('sections', [])]
        if not page['hold'] and sections and all(call['budget']['fits'] for _, call in sections):
            pending.extend(sections)
            continue
        held.append({
            'page': page['view']['id'], 'unresolved': page['hold'], 'single_budget': row['budget'],
            'reason': 'required_material_unresolved' if page['hold'] else 'bounded_page_detail_required',
        })
    planned = []
    while pending:
        first, single = pending.pop(0)
        chosen = single
        if not first['images']:
            peers = sorted(range(len(pending)), key=lambda i: pending[i][1]['budget']['input_size'], reverse=True)
            for i in peers:
                peer, peer_single = pending[i]
                if peer['images'] or (single['budget']['estimated_output_tokens']
                                     + peer_single['budget']['estimated_output_tokens'] > USABLE_OUTPUT):
                    continue
                pair = body_request([first, peer], limit)
                if pair['budget']['fits']:
                    chosen = pair
                    pending.pop(i)
                    break
        planned.append(chosen)
    return planned, held


def check_page(row, slot, answer):
    """Per-slot receipt and program Build check, without independent semantic review."""
    require(row['budget']['fits'], 'page request did not fit its pinned budget')
    _check_shape(PAGE_RESULT, answer, {}, '$')
    view = row['view']['pages'][slot]
    state = 'held'
    if 'blocked_reason' not in answer:
        allowed = set(view['materials'])
        inline = set(re.findall(r'\[(S[0-9]+-[AB][0-9]+)\]', answer['content']))
        citations = set(answer['citations'])
        require(answer['content'].strip() and citations == inline <= allowed
                and set(view['required_citations']) <= citations, 'invalid or incomplete inline citation binding')
        require(set(answer['inspected_assets']) == set(view['required_assets']), 'required asset inspection is incomplete')
        state = 'qa_held' if any(q['review_required'] for q in view['qa'].values()) else 'structurally_valid'
    body = {'contract': CONTRACT, 'request_sha256': row['id'], 'slot': slot, 'page_sha256': fingerprint(view),
            'page_id': view['id'], 'page_section': view.get('page_section'),
            'answer': answer, 'state': state,
            'independent_review': 'not_performed', 'publication_eligible': False}
    return {**body, 'receipt_sha256': fingerprint(body)}


def build_sections(checks):
    """Assemble complete written sections under one existing logical page."""
    grouped = {}
    for row in checks:
        if row['page_section']:
            grouped.setdefault(row['page_id'], []).append(row)
    result = {}
    for page_id, rows in grouped.items():
        rows.sort(key=lambda row: row['page_section']['number'])
        count = rows[0]['page_section']['total']
        if len(rows) != count or any(row['state'] == 'held' for row in rows):
            continue
        require([row['page_section']['number'] for row in rows] == list(range(1, count + 1)),
                'missing or duplicate page sections')
        result[page_id] = {
            'content': '\n\n'.join(
                f"## {row['page_section']['name']}\n\n{row['answer']['content']}" for row in rows
            ),
            'section_receipts': [row['receipt_sha256'] for row in rows],
            'state': 'qa_held' if any(row['state'] == 'qa_held' for row in rows) else 'structurally_valid',
            'publication_eligible': False,
        }
    return result
