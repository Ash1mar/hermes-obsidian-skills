"""Form physical writing fragments without altering semantic page identity."""
import copy
import re
import unicodedata

from hermes_source_units import ContractError
from hermes_source_units.short_build import project_refs
from hermes_source_units.validation import fingerprint
from short_evidence_pass import USABLE_OUTPUT, evidence, require, source_revision
from short_knowledge_plan import CONTRACT as KNOWLEDGE_PLAN, SCOPE_CONTRACT
from short_knowledge_plan import enabled as knowledge_plan, matching_pages, organization_topics, synthesis_id, synthesis_topics
from short_page_contract import CONTRACT, WRITE_CONTRACT, body_request, local_task, obligations, qa_impacts, topic_items, writing_view

SHARED = {'condition', 'exception', 'conflict', 'contrary', 'definition', 'context'}
_UNIT_BOUNDARY = re.compile(r'^(?:#{1,6}\s|\d+(?:\.\d+)+(?:\s|[、.)])|\d+[、.)]\s|第[一二三四五六七八九十\d]+[条节章])')


def connected_groups(source, relations, *, join_adjacent=True):
    """Keep reference edges and whole registered assets within the same unit of work."""
    shared = SHARED if join_adjacent else SHARED - {'definition'}
    common = {row['item'] for row in relations if row['role'] in shared and not row['applies_to']}
    owned = {row['item'] for row in relations if row['role'] not in shared or row['applies_to']}
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
        if (join_adjacent and previous is not None and item.get('section') == items[previous].get('section')
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
    scoped = (reduced.get('card_layout') or {}).get('scope_contract') == SCOPE_CONTRACT
    owning_scopes = list(dict.fromkeys(topic['scope'] for topic in topics.values()))
    topics = {key: {**topic, 'relations': list(topic['relations'])} for key, topic in topics.items()}
    related = reduced['topics'] if reduced.get('bounded_coordination') else page['topics']
    for tid in related:
        original = reduced['topics'][tid]
        for link in original['relations']:
            if link['role'] not in SHARED or not primary.intersection(link['applies_to']):
                continue
            if scoped and link['role'] == 'context':
                link = {**link, 'applies_to': [ref for ref in link['applies_to'] if ref in primary]}
            part = topics.setdefault(tid, {**original, 'relations': []})
            if link not in part['relations']:
                part['relations'].append(link)
    selection = topic_items(topics)
    relations = [link for topic in topics.values() for link in topic['relations']]
    context_only = ({link['item'] for link in relations if link['role'] == 'context'}
                    - {link['item'] for link in relations if link['role'] != 'context'}
                    - {ref for link in relations if link['role'] != 'context' for ref in link['applies_to']}) if scoped else set()
    required_support = {link['item'] for link in relations
                        if link['role'] in {'condition', 'exception', 'definition', 'conflict', 'contrary'}
                        and primary.intersection(link['applies_to'])} if scoped else set()
    material, images, raw_qa, revisions = {}, [], {}, {}
    for key, ids in selection.items():
        source = sources[key]
        supplied, pictures = evidence(source, ids, context_only=context_only)
        material.update(supplied)
        images.extend(pictures)
        raw_qa[key] = source['qa']
        revisions[key] = source_revision(source)
    modern = reduced.get('writing_contract') == WRITE_CONTRACT
    card = reduced.get('card_layout')
    duties = obligations(sources, material, topics, primary | required_support, modern=modern, card=bool(card))
    for key in context_only:
        duties[key].update(role='context', express=False, inspect=False, covers_by=[], assignment='shared')
    basis_refs = set((page.get('project_basis') or {}).get('refs', ())) & primary
    synthesis = page.get('synthesis_basis')
    synthesis_refs = {row['ref'] for row in synthesis['anchors']} if synthesis else set()
    for key in basis_refs | synthesis_refs:
        duties[key].update(role='substantive', express=True, covers_by=[])
    impacts = qa_impacts(sources, selection, material, duties)
    for impact in impacts:
        if impact['items'] and set(impact['items']) <= context_only:
            impact.update(writing='allow', publication='allow')
    anchors = {ref for link in relations if not scoped or link['role'] != 'context' for ref in link['applies_to']}
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
    if scoped:
        view.update(scope_contract=SCOPE_CONTRACT, assigned_topic_scopes=owning_scopes,
                    context_only=sorted(context_only))
    if 'page_type' in page:
        view['page_type'] = page['page_type']
        if basis_refs:
            view['project_basis_refs'] = sorted(basis_refs)
    if 'knowledge_plan' in page:
        view['knowledge_plan'] = page['knowledge_plan']
    if synthesis:
        view['synthesis_task'] = {'question': page['reader_question'], 'scope': page['scope'],
                                  'anchors': sorted(synthesis_refs), 'entries': synthesis['entries']}
        if 'purpose' in synthesis:
            view['synthesis_task']['purpose'] = synthesis['purpose']
        if 'organization_units' in page:
            view['synthesis_task']['reading_steps'] = [entry['short_id'] for entry in synthesis['entries']]
    if card:
        view['card_contract'] = card['contract']
        if card.get('local_fragment_scope'):
            local_task(view, {key: {ref: material[ref]['label'] for ref in item['required_assets']}
                              for key, item in material.items() if item['required_assets']})
    view['fragment_id'] = fingerprint([CONTRACT, view])
    return {'view': view, 'images': images, 'hold': sorted(hold)}


def selected_topics(reduced, keys):
    return {key: reduced['topics'][key] for key in keys}


def primary_items(topics):
    return {link['item'] for topic in topics.values() for link in topic['relations']}


def fits(fragment, limit):
    return body_request([fragment], limit)['budget']['fits']


def split_topic(sources, reduced, page, position, tid, topic, limit, *, isolate_holds=False):
    source = sources[topic['source']]
    common, groups = connected_groups(source, topic['relations'], join_adjacent=not isolate_holds)
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
        if pending:
            number = len(result) + 1
            candidate = part(trial, number)
            separated = isolate_holds and (bool(part(pending, number)['hold']) != bool(part(group, number)['hold']))
            if separated or not fits(candidate, limit):
                result.append(part(pending, number))
                trial = group
        pending = trial
    if pending:
        result.append(part(pending, len(result) + 1))
    return result


def organization_fragments(sources, reduced, page, limit):
    """Keep all anchors for a reading step together, and assemble steps under one page ID."""
    basis = page['synthesis_basis']
    entries = basis['entries']
    units = page['organization_units']

    def part(steps, index):
        anchors = [anchor for entry in steps for anchor in units[entry['short_id']]]
        topics = organization_topics(reduced, anchors)
        name = page['name'] if len(steps) == len(entries) else ' → '.join(entry['name'] for entry in steps)
        local = {**page, 'topics': list(topics), 'synthesis_basis': {**basis, 'anchors': anchors, 'entries': steps}}
        return make_fragment(sources, {**reduced, 'topics': topics}, local,
                             {'section': index, 'name': name, 'part': 0}, topics, primary_items(topics))

    result, pending = [], []
    for entry in entries:
        if pending:
            trial = part([*pending, entry], len(result) + 1)
            if trial['hold'] or not fits(trial, limit):
                result.append(part(pending, len(result) + 1))
                pending = []
        pending.append(entry)
    if pending:
        result.append(part(pending, len(result) + 1))
    return result


def page_fragments(sources, reduced, page, limit):
    """Fit each confirmed section independently; finer cuts keep original evidence dependencies."""
    if 'organization_units' in page:
        return organization_fragments(sources, reduced, page, limit)
    if 'synthesis_basis' in page:
        # A bounded relationship requires its complete evidence packet in one slot.
        topics = synthesis_topics(reduced, page)
        position = {'section': 1, 'name': page['name'], 'part': 0}
        return [make_fragment(sources, {**reduced, 'topics': topics}, page, position, topics, primary_items(topics))]
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
            isolate = bool(reduced.get('card_layout') and one['hold'])
            if not isolate and fits(one, limit):
                result.append(one)
            else:
                result.extend(split_topic(sources, reduced, page, pos, tid, topic, limit, isolate_holds=isolate))
    return result


def split_atomic_output(fragment, limit):
    """Split output ownership while duplicating the entire input dependency closure."""
    original = fragment['view']
    if 'synthesis_task' in original:
        return []
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
        if 'project_basis_refs' in view:
            view['project_basis_refs'] = [ref for ref in view['project_basis_refs'] if ref in scope]
        view['fragment_id'] = fingerprint([CONTRACT, {k: v for k, v in view.items() if k != 'fragment_id'}])
        part = {**fragment, 'view': view}
        if not fits(part, limit):
            return []
        parts.append(part)
    return parts


def page_identity(identities, namespace):
    ordered = sorted(identities)
    return fingerprint([namespace, ordered] if namespace else ordered)


def spec_identity(reduced, page, namespace):
    if 'synthesis_basis' in page:
        return synthesis_id(reduced, page, namespace)
    return page_identity((reduced['topics'][tid]['identity'] for tid in page['topics']), namespace)


def page_metadata(page):
    return {key: page[key] for key in ('page_type', 'project_basis', 'knowledge_plan', 'synthesis_basis',
                                      'existing_page_hints', 'navigation_theme')
            if page.get(key) is not None}


def project_supported(sources, reduced, page):
    """A project name must occur in its own mapped text, not just a plan title."""
    basis = page.get('project_basis')
    try:
        refs = project_refs(basis)
    except ContractError:
        return False
    offered = {link['item'] for tid in page['topics'] for link in reduced['topics'][tid]['relations']
               if link['role'] in ('evidence', 'definition')}
    if not refs <= offered:
        return False
    items = [sources[ref.split('-', 1)[0]]['items'][ref] for ref in refs]

    def normalized(text):
        return ' '.join(unicodedata.normalize('NFC', text).casefold().split())

    return (all(item['kind'] == 'block' and item['text'] for item in items)
            and any(normalized(basis['name']) in normalized(item['text']) for item in items))


def pack(sources, reduced, specs, limit, *, split_output=False, oversized=None, page_namespace=None):
    pending, planned, held, expected = [], [], [], {}
    pages = [{**spec, 'id': spec_identity(reduced, spec, page_namespace)} for spec in specs]
    layout = reduced.get('card_layout')
    if layout:
        from short_cards import label, place
        from short_navigation import CONTRACT as NAVIGATION_CONTRACT
        pages = place(layout, pages)
    for page in pages:
        page_id = page['id']
        if knowledge_plan(layout):
            page['knowledge_plan'] = KNOWLEDGE_PLAN
        if layout and layout.get('navigation_contract') == NAVIGATION_CONTRACT:
            page['navigation_theme'] = label(page.get('theme_directory') or page['name'])
        expected[page_id] = {'name': page['name'], 'reader_question': page['reader_question'],
                             'scope': page['scope'], 'fragments': [], **page_metadata(page)}
        if knowledge_plan(layout):
            matches = matching_pages(layout, [page['name']], short_id=page_id)
            if matches:
                expected[page_id]['existing_page_hints'] = matches
        if 'registered_path' in page:
            held.append({'page': page_id, 'reason': 'registered_page_requires_revision', 'path': page['registered_path']})
            continue
        if 'card_path' in page:
            expected[page_id]['card_path'] = page['card_path']
        if page.get('page_type') == 'project' and not project_supported(sources, reduced, page):
            held.append({'page': page_id, 'reason': 'project_basis_not_bound_to_mapped_source'})
            continue
        fragments = page_fragments(sources, reduced, page, limit)
        if split_output:
            fragments = [part for fragment in fragments for part in (split_atomic_output(fragment, limit) or [fragment])]
        expected[page_id]['fragments'] = [f['view']['fragment_id'] for f in fragments]
        require(len(expected[page_id]['fragments']) == len(set(expected[page_id]['fragments'])),
                'duplicate fragment identity')
        for fragment in fragments:
            single = body_request([fragment], limit)
            view = fragment['view']
            if fragment['hold'] or not single['budget']['fits']:
                synthesis = 'synthesis_task' in view
                if oversized is not None and not fragment['hold'] and not synthesis:
                    oversized[view['fragment_id']] = fragment
                held.append({'page': page_id, 'fragment_id': view['fragment_id'], 'position': view['fragment_position'],
                             'reason': ('required_material_unresolved' if fragment['hold'] else
                                        'synthesis_evidence_over_budget' if synthesis else 'atomic_evidence_over_budget'),
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
        if derived := page.get('derived_from'):
            parent = expected[derived['page_id']]
            require(all(row['page_id'] == derived['page_id'] for row in rows)
                    and set(page['fragments']) <= set(parent['fragments'])
                    and all(row['fragment_position']['section'] == derived['section'] for row in rows)
                    and derived['receipt_sha256'] == [row['receipt_sha256'] for row in rows],
                    'independent section lost its exact parent native proof')
        sections = {}
        for row in rows:
            pos = row['fragment_position']
            sections.setdefault((pos['section'], pos['name']), []).extend(row['answer']['sections'])
        if page.get('page_type') == 'project':
            cited = {ref for fragments in sections.values() for fragment in fragments
                     for ref in re.findall(r'\[(S[0-9]+-B[0-9]+)\]', fragment['content'])}
            if not project_refs(page.get('project_basis')) <= cited:
                continue
        complete[page_id] = {
            'contract': CONTRACT, 'name': page['name'], 'reader_question': page['reader_question'], 'scope': page['scope'],
            **page_metadata(page),
            'content': '\n\n'.join(f'## {name}\n\n' + '\n\n'.join(
                f"### {body['heading']}\n\n{body['content']}" for body in bodies)
                for (_, name), bodies in sections.items()),
            'fragment_receipts': [row['receipt_sha256'] for row in rows],
            'qa_publication_review': sorted({qid for row in rows for qid in row['qa_publication_review']}),
            'publication_eligible': False,
        }
        if 'card_path' in page:
            from short_cards import render
            complete[page_id].update(card_path=page['card_path'], content=render(page, sections))
        if page.get('derived_from'):
            complete[page_id]['derived_from'] = page['derived_from']
        if coverage:
            complete[page_id]['coverage_receipts'] = [row['coverage_sha256'] for row in coverage]
    return complete


def independent_sections(expected, checks, covered, specs, reduced, planned, held):
    """Deliver whole confirmed sections only; never salvage arbitrary successful pieces."""
    from short_cards import place

    expected = copy.deepcopy(expected)
    complete = build(expected, checks, covered)
    by_id = {row['fragment_id']: row for row in checks}
    views = {v['fragment_id']: v for row in planned for v in row['view']['pages'].values()}
    covered_ids = {row['fragment_id'] for row in covered}
    positions = {row['fragment_id']: row['position'] for row in held if row.get('fragment_id') and row.get('position')}
    positions.update({fid: view['fragment_position'] for fid, view in views.items()})
    proposals = []
    for spec in specs:
        if 'synthesis_basis' in spec:
            continue
        pid = spec_identity(reduced, spec, reduced.get('page_namespace'))
        if pid not in expected or pid in complete or spec['grouping_basis'] in ('inseparable_conditions', 'comparison'):
            continue
        parent = expected[pid]
        if not parent.get('card_path') or parent.get('page_type') == 'project':
            continue
        # Missing/over-budget pieces are not present in views, so their section can never be complete.
        for number, section in enumerate(spec['sections'], 1):
            members = [fid for fid in parent['fragments'] if positions.get(fid, {}).get('section') == number]
            rows = [by_id.get(fid) for fid in members]
            if (not members or any(fid not in positions for fid in parent['fragments']) or covered_ids.intersection(members)
                    or any(row is None or row['state'] != 'structurally_valid' for row in rows)):
                continue
            material = {ref for fid in members for ref in views[fid]['materials']}
            relations = [link for tid in section['topics'] for link in reduced['topics'][tid]['relations']]
            edges = {(link['item'], dep) for link in relations for dep in link['applies_to']}
            if not all({item, dep} <= material for item, dep in edges):
                continue
            # Definitions, conditions, exceptions and all source dependencies assigned to this section remain mandatory.
            if not {link['item'] for link in relations} <= material:
                continue
            scopes = list(dict.fromkeys(reduced['topics'][tid]['scope'] for tid in section['topics']))
            identity = fingerprint(['short-independent-section/v1', pid, number,
                                    sorted(reduced['topics'][tid]['identity'] for tid in section['topics'])])
            proposals.append({'id': identity, 'name': section['name'], 'scope': '；'.join(scopes),
                              'reader_question': f"关于「{section['name']}」，原文有哪些规定和依据？",
                              'theme_directory': spec['theme_directory'], 'fragments': members,
                              **({'page_type': 'card'} if 'page_type' in parent else {}),
                              **({'knowledge_plan': KNOWLEDGE_PLAN} if 'knowledge_plan' in parent else {}),
                              **({'navigation_theme': parent['navigation_theme']} if 'navigation_theme' in parent else {}),
                              'derived_from': {'contract': 'short-independent-section/v1', 'page_id': pid,
                                               'section': number, 'topics': section['topics'],
                                               'receipt_sha256': [row['receipt_sha256'] for row in rows],
                                               'dependency_edges': [list(edge) for edge in sorted(edges)]}})
    layout = copy.deepcopy(reduced['card_layout'])
    layout['occupied'] = sorted(set(layout['occupied']) | {p['card_path'] for p in expected.values() if p.get('card_path')})
    for page in place(layout, proposals):
        if 'registered_path' in page and 'page_type' in page:
            expected[page['id']] = {'name': page['name'], 'page_type': page['page_type'], 'fragments': []}
            held.append({'page': page['id'], 'reason': 'registered_page_requires_revision',
                         'path': page['registered_path']})
            continue
        require('registered_path' not in page, 'independent section identity already registered')
        expected[page['id']] = {k: v for k, v in page.items() if k not in ('id', 'theme_directory')}
    return expected
