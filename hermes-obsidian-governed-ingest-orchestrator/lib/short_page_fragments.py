"""Form physical writing fragments without altering semantic page identity."""
import copy
import re

from hermes_source_units.validation import fingerprint
from short_evidence_pass import USABLE_OUTPUT, evidence, require, source_revision
from short_page_contract import CONTRACT, WRITE_CONTRACT, body_request, obligations, qa_impacts, topic_items, writing_view

SHARED = {'condition', 'exception', 'conflict', 'contrary', 'definition', 'context'}
_UNIT_BOUNDARY = re.compile(r'^(?:#{1,6}\s|\d+(?:\.\d+)+(?:\s|[、.)])|\d+[、.)]\s|第[一二三四五六七八九十\d]+[条节章])')


def connected_groups(source, relations):
    """Keep reference edges and whole registered assets within the same unit of work."""
    common = {row['item'] for row in relations if row['role'] in SHARED and not row['applies_to']}
    owned = {row['item'] for row in relations if row['role'] not in SHARED or row['applies_to']}
    items = source['items']
    parents = {key: key for key in owned}

    def find(key):
        while parents[key] != key:
            key = parents[key]
        return key

    def join(left, right):
        if left in owned and right in owned:
            parents[find(right)] = find(left)

    previous = None
    for key, item in items.items():
        if key not in owned or item['kind'] != 'block':
            continue
        text = re.sub(r'<!--[^<>]*-->', '', item['text'] or '').lstrip()
        if (previous is not None and item.get('section') == items[previous].get('section')
                and not item['required_assets'] and not _UNIT_BOUNDARY.match(text)):
            join(previous, key)
        previous = key
    for row in relations:
        item = row['item']
        for dep in (*row['applies_to'], *items[item]['required_assets']):
            join(item, dep)
    components = {}
    for key in items:
        if key in owned:
            components.setdefault(find(key), set()).add(key)
    return common, list(components.values())


def make_fragment(sources, reduced, page, position, topics, primary):
    # An anchored exception or condition from a sibling topic follows its target.
    topics = {key: {**topic, 'relations': list(topic['relations'])} for key, topic in topics.items()}
    for tid in page['topics']:
        original = reduced['topics'][tid]
        for link in original['relations']:
            if link['role'] not in SHARED or not primary.intersection(link['applies_to']):
                continue
            part = topics.setdefault(tid, {**original, 'relations': []})
            if link not in part['relations']:
                part['relations'].append(link)
    selection = topic_items(topics)
    material, images, raw_qa, revisions = {}, [], {}, {}
    for key, ids in selection.items():
        source = sources[key]
        supplied, pictures = evidence(source, ids)
        material.update(supplied)
        images.extend(pictures)
        raw_qa[key] = source['qa']
        revisions[key] = source_revision(source)
    modern = reduced.get('writing_contract') == WRITE_CONTRACT
    duties = obligations(sources, material, topics, primary, modern=modern)
    impacts = qa_impacts(sources, selection, material, duties)
    anchors = {ref for topic in topics.values() for relation in topic['relations'] for ref in relation['applies_to']}
    hold = {item for item, duty in duties.items()
            if (duty['express'] or duty['inspect'] or item in anchors)
            and reduced['dispositions'][item]['status'] != 'linked'}
    hold.update(item for impact in impacts if impact['writing'] == 'hold' for item in impact['items'])
    view = {
        'id': page['id'], 'name': page['name'], 'scope': page['scope'],
        'reader_question': page['reader_question'], 'fragment_position': position,
        'source_revisions': revisions, 'original_qa': raw_qa, 'qa_impact': impacts,
        'topics': {key: {k: v[k] for k in ('name', 'scope', 'relations')} for key, v in topics.items()},
        'materials': material, 'obligations': duties,
    }
    if modern:
        writing_view(view)
    view['fragment_id'] = fingerprint([CONTRACT, view])
    return {'view': view, 'images': images, 'hold': sorted(hold)}


def selected_topics(reduced, keys):
    return {key: reduced['topics'][key] for key in keys}


def primary_items(topics):
    return {link['item'] for topic in topics.values() for link in topic['relations']}


def fits(fragment, limit):
    return body_request([fragment], limit)['budget']['fits']


def split_topic(sources, reduced, page, position, tid, topic, limit):
    source = sources[topic['source']]
    common, groups = connected_groups(source, topic['relations'])
    if len(groups) < 2:
        return [make_fragment(sources, reduced, page, position, {tid: topic}, primary_items({tid: topic}))]

    def part(items, number):
        # Shared context is present in every request and has one primary owner.
        selected = {**topic, 'relations': [link for link in topic['relations'] if link['item'] in items | common]}
        return make_fragment(sources, reduced, page, {**position, 'subpart': number}, {tid: selected},
                             items | common if number == 1 else items)

    result, pending = [], set()
    for group in groups:
        trial = pending | group
        if pending and not fits(part(trial, len(result) + 1), limit):
            result.append(part(pending, len(result) + 1))
            trial = group
        pending = trial
    if pending:
        result.append(part(pending, len(result) + 1))
    return result


def page_fragments(sources, reduced, page, limit):
    """Fit each confirmed section independently; finer cuts keep original evidence dependencies."""
    result = []
    sections = page['sections'] or [{'name': page['name'], 'topics': page['topics']}]
    for number, section in enumerate(sections, 1):
        position = {'section': number, 'name': section['name'], 'part': 0}
        topics = selected_topics(reduced, section['topics'])
        fragment = make_fragment(sources, reduced, page, position, topics, primary_items(topics))
        if fits(fragment, limit) and not fragment['hold']:
            result.append(fragment)
            continue
        for part, (tid, topic) in enumerate(topics.items(), 1):
            pos = {**position, 'part': part, 'topic': topic['name']}
            one = make_fragment(sources, reduced, page, pos, {tid: topic}, primary_items({tid: topic}))
            if fits(one, limit):
                result.append(one)
            else:
                result.extend(split_topic(sources, reduced, page, pos, tid, topic, limit))
    return result


def split_atomic_output(fragment, limit):
    """Split output ownership while duplicating the entire input dependency closure."""
    original = fragment['view']
    budget = body_request([fragment], limit)['budget']
    if (fragment['hold'] or budget['fits'] or budget['input_size'] > budget['input_cap']
            or budget['estimated_output_tokens'] <= budget['usable_output_tokens']):
        return []
    duties, materials = original['obligations'], original['materials']
    primary = [key for key in materials if duties[key]['assignment'] == 'primary']
    tables = {key for key in primary if materials[key]['kind'] == 'asset'}
    table_fields = tables | {key for key in primary if tables.intersection(materials[key]['required_assets'])
                             and not set(duties[key]['mapped_roles']) & SHARED}
    scopes = ([key for key in primary if key not in table_fields],
              [key for key in primary if key in table_fields])
    if not all(any(duties[key]['express'] or duties[key]['covers_by'] for key in scope) for scope in scopes):
        return []
    parts = []
    for number, (name, scope) in enumerate(zip(('text_requirements', 'whole_asset_coverage'), scopes), 1):
        view = copy.deepcopy(original)
        if view.get('writing_contract') != WRITE_CONTRACT:
            writing_view(view)
        view['fragment_position']['output_part'] = number
        view['writing_assignment'] = {'scope': name, 'primary': scope, 'original_fragment': original['fragment_id']}
        for key, duty in view['obligations'].items():
            if key not in scope:
                duty.update(assignment='shared', express=False)
        view['fragment_id'] = fingerprint([CONTRACT, {k: v for k, v in view.items() if k != 'fragment_id'}])
        part = {**fragment, 'view': view}
        if not fits(part, limit):
            return []
        parts.append(part)
    return parts


def pack(sources, reduced, specs, limit, *, split_output=False, oversized=None):
    pending, planned, held, expected = [], [], [], {}
    for spec in specs:
        topics = selected_topics(reduced, spec['topics'])
        page = {**spec, 'id': fingerprint(sorted(topic['identity'] for topic in topics.values()))}
        fragments = page_fragments(sources, reduced, page, limit)
        if split_output:
            fragments = [part for fragment in fragments for part in (split_atomic_output(fragment, limit) or [fragment])]
        page_id = page['id']
        expected[page_id] = {'name': page['name'], 'reader_question': page['reader_question'],
                             'scope': page['scope'], 'fragments': [f['view']['fragment_id'] for f in fragments]}
        require(len(expected[page_id]['fragments']) == len(set(expected[page_id]['fragments'])),
                'duplicate fragment identity')
        for fragment in fragments:
            single = body_request([fragment], limit)
            view = fragment['view']
            if fragment['hold'] or not single['budget']['fits']:
                if oversized is not None and not fragment['hold']:
                    oversized[view['fragment_id']] = fragment
                held.append({'page': page_id, 'fragment_id': view['fragment_id'], 'position': view['fragment_position'],
                             'reason': 'required_material_unresolved' if fragment['hold'] else 'atomic_evidence_over_budget',
                             'unresolved': fragment['hold'], 'material_ids': list(view['materials']),
                             'assets': [key for key, duty in view['obligations'].items() if duty['inspect']],
                             'budget': single['budget']})
            else:
                pending.append((fragment, single))
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
    return planned, held, expected


def build(expected, checks, covered=(), *, excluded_receipts=()):
    require(not {row['receipt_sha256'] for row in checks}.intersection(excluded_receipts)
            and not {row['donor_receipt_sha256'] for row in covered}.intersection(excluded_receipts),
            'excluded fragment cannot be assembled or reused as coverage')
    by_id = {row['fragment_id']: row for row in checks}
    require(len(by_id) == len(checks), 'duplicate fragment receipt')
    reused = {row['fragment_id']: row for row in covered}
    require(len(reused) == len(covered) and not reused.keys() & by_id.keys(), 'conflicting fragment coverage receipts')
    complete = {}
    for page_id, page in expected.items():
        coverage = [reused[ref] for ref in page['fragments'] if ref in reused]
        for row in coverage:
            donor = by_id.get(row['donor_fragment_id'])
            require(row['page_id'] == page_id and donor is not None and donor['page_id'] == page_id
                    and donor['fragment_id'] in page['fragments']
                    and donor['receipt_sha256'] == row['donor_receipt_sha256']
                    and fingerprint({k: v for k, v in row.items() if k != 'coverage_sha256'}) == row['coverage_sha256'],
                    'invalid same-page coverage receipt')
        rows = [by_id.get(ref) for ref in page['fragments'] if ref not in reused]
        if not rows or any(row is None or row['state'] != 'structurally_valid' for row in rows):
            continue
        sections = {}
        for row in rows:
            pos = row['fragment_position']
            sections.setdefault((pos['section'], pos['name']), []).extend(row['answer']['sections'])
        complete[page_id] = {
            'contract': CONTRACT, 'name': page['name'], 'reader_question': page['reader_question'], 'scope': page['scope'],
            'content': '\n\n'.join(f'## {name}\n\n' + '\n\n'.join(
                f"### {body['heading']}\n\n{body['content']}" for body in bodies)
                for (_, name), bodies in sections.items()),
            'fragment_receipts': [row['receipt_sha256'] for row in rows],
            'qa_publication_review': sorted({qid for row in rows for qid in row['qa_publication_review']}),
            'publication_eligible': False,
        }
        if coverage:
            complete[page_id]['coverage_receipts'] = [row['coverage_sha256'] for row in coverage]
    return complete
