"""Independent page ownership and bounded reuse of mapped evidence in syntheses."""
import unicodedata

from hermes_source_units.validation import fingerprint

CONTRACT = 'short-knowledge-plan/v1'
SCOPE_CONTRACT = 'short-page-scope/v1'
ENTRY_TYPES = {'card', 'concept', 'entity'}
ANCHOR_ROLES = {'evidence', 'definition', 'asset'}
DEPENDENCY_ROLES = {'condition', 'exception', 'definition', 'conflict', 'contrary', 'asset'}
MAX_ANCHORS = 8


def enabled(layout):
    return (layout or {}).get('knowledge_plan') == CONTRACT


def title_key(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def matching_pages(layout, titles, *, short_id=None, limit=8):
    """Return bounded registry hints. A name match never authorizes a merge."""
    names = {title_key(name) for name in titles}
    hits = []
    for page in layout.get('registered_pages', {}).values():
        exact = short_id is not None and page.get('short_id') == short_id
        if exact or title_key(page['name']) in names:
            hits.append({**page, 'match': 'registered_identity' if exact else 'name_only'})
    hits.sort(key=lambda row: (row['match'] != 'registered_identity', row['page_id']))
    return hits[:limit]


def anchor_options(topic):
    return list(dict.fromkeys(link['item'] for link in topic['relations']
                              if link['role'] in ANCHOR_ROLES))[:MAX_ANCHORS]


def synthesis_id(reduced, page, namespace):
    members = sorted(reduced['topics'][tid]['identity'] for tid in page['topics'])
    return fingerprint([CONTRACT, 'topic', namespace, members])


def target_issues(topics, owners, anchors, allowed, entry_id):
    issues = []
    for anchor in anchors:
        tid, ref = anchor['topic'], anchor['ref']
        owner = owners.get(tid)
        target = dict(anchor)
        if owner and all(owner.get(field, '').strip() for field in ('name', 'scope', 'reader_question')):
            target.update(short_id=entry_id(owner), page_type=owner['page_type'])
            if 'synthesis_basis' in owner or 'derived_from' in owner:
                reason = 'target_is_derived_topic'
            elif owner['page_type'] not in allowed:
                reason = 'target_type_not_allowed'
            elif tid not in topics or ref not in anchor_options(topics[tid]):
                reason = 'anchor_not_owned_or_mapped'
            else:
                continue
        else:
            reason = 'target_owner_missing'
        issues.append({**target, 'reason': reason})
    return issues


def syntheses(reduced, proposed, entries, *, namespace=None, entry_id):
    """Keep failed proposals local and leave the primary coverage partition intact."""
    topics = reduced['topics']
    owners = {key: page for page in entries for key in page['topics']}
    specs, gaps, seen, organized = [], [], set(), set()
    layout = reduced.get('card_layout') or {}
    versioned = layout.get('organization_targets')
    for row in proposed:
        anchors = row['anchors']
        pairs = {(a['topic'], a['ref']) for a in anchors}
        members = sorted({a['topic'] for a in anchors})
        organization = row.get('purpose') == 'organization'
        required_targets = organization and versioned
        errors = []
        if (not all(row[field].strip() for field in ('name', 'scope', 'reader_question', 'theme_directory'))
                or not 2 <= len(anchors) <= MAX_ANCHORS or len(pairs) != len(anchors)
                or len({a['ref'] for a in anchors}) < 2):
            reason = 'synthesis_requires_distinct_bounded_anchors_and_scope'
        elif versioned and (errors := target_issues(topics, owners, anchors,
                                  ENTRY_TYPES | {'topic'} if organization else ENTRY_TYPES, entry_id)):
            reason = errors[0]['reason']
        elif not versioned and any(key not in owners or owners[key]['page_type'] not in ENTRY_TYPES
                 or ref not in anchor_options(topics[key]) for key, ref in pairs):
            reason = 'synthesis_anchor_lacks_independent_owner_or_mapped_evidence'
        else:
            related = {entry_id(owners[key]): owners[key] for key in members}
            order = sorted(related)
            if organization:
                order = list(dict.fromkeys(entry_id(owners[a['topic']]) for a in anchors))
            spec = {**{key: row[key] for key in ('name', 'scope', 'reader_question', 'theme_directory')},
                    'page_type': 'topic', 'topics': members, 'sections': [],
                    'grouping_basis': 'shared_reader_task', 'grouping_reason': 'A scoped synthesis of independent pages.',
                    'synthesis_basis': {'anchors': sorted(anchors, key=lambda a: (a['topic'], a['ref'])),
                                        'entries': [{'short_id': key, **{field: related[key][field]
                                                     for field in ('name', 'scope', 'page_type')}}
                                                    for key in order]}}
            if 'purpose' in row:
                spec['synthesis_basis']['purpose'] = row['purpose']
            if required_targets:
                spec['synthesis_basis'].update(anchors=anchors, required_entries=True)
                for entry in spec['synthesis_basis']['entries']:
                    entry['anchors'] = [anchor for anchor in anchors
                                        if entry_id(owners[anchor['topic']]) == entry['short_id']]
            if organization and layout.get('organization_fragments'):
                units = {key: [] for key in order}
                for anchor in anchors:
                    units[entry_id(owners[anchor['topic']])].append(anchor)
                spec['organization_units'] = units
            key = synthesis_id(reduced, spec, namespace)
            targets = frozenset(related)
            if len(related) < 2:
                reason = 'insufficient_distinct_targets' if versioned else 'synthesis_requires_two_independent_pages'
                if versioned:
                    errors = [{**anchor, 'short_id': entry_id(owners[anchor['topic']]), 'reason': reason}
                              for anchor in anchors]
            elif key in targets:
                reason = 'target_is_derived_topic'
            elif key in seen or required_targets and targets in organized:
                reason = 'duplicate_synthesis_membership'
            else:
                seen.add(key)
                if required_targets:
                    organized.add(targets)
                specs.append(spec)
                continue
        gaps.append({'name': row['name'], 'topics': members, 'anchors': anchors, 'reason': reason,
                     **({'target_issues': errors} if errors else {})})
    return specs, gaps


def synthesis_topics(reduced, page):
    """Expand anchor evidence and attached conditions using the existing Map relations."""
    members = set(page['topics'])
    active = {a['ref'] for a in page['synthesis_basis']['anchors']}
    selected = {}
    while True:
        before = len(active)
        for tid, topic in reduced['topics'].items():
            for link in topic['relations']:
                deps = link['applies_to']
                role = link['role']
                if (link['item'] in active
                        or (role in DEPENDENCY_ROLES | {'context'} and active.intersection(deps))
                        or (role in DEPENDENCY_ROLES and tid in members and not deps)):
                    target = selected.setdefault(tid, {**topic, 'relations': []})
                    if link not in target['relations']:
                        target['relations'].append(link)
                    active.add(link['item'])
                    active.update(deps)
        if len(active) == before:
            return selected


def organization_topics(reduced, anchors):
    """Follow scoped requirements without traversing neighboring entries' context."""
    members = {anchor['topic'] for anchor in anchors}
    anchor_refs = {anchor['ref'] for anchor in anchors}
    active = set(anchor_refs)
    selected = {}
    for tid, topic in reduced['topics'].items():
        for link in topic['relations']:
            if (tid in members and link['role'] in DEPENDENCY_ROLES - {'asset'}
                    and not link['applies_to']):
                active.add(link['item'])
    while True:
        previous = active.copy()
        for tid, topic in reduced['topics'].items():
            for link in topic['relations']:
                role, ref, deps = link['role'], link['item'], link['applies_to']
                applicable = active.intersection(deps)
                if role == 'context' or not (ref in active or role in DEPENDENCY_ROLES and applicable):
                    continue
                if role in DEPENDENCY_ROLES and deps:
                    if not applicable and ref not in anchor_refs:
                        continue
                    link = {**link, 'applies_to': [dep for dep in deps if dep in applicable or ref in anchor_refs]}
                active.add(ref)
                if ref in anchor_refs:
                    active.update(deps)
                group = selected.setdefault(tid, {**topic, 'relations': {}})
                group['relations'][(ref, role, tuple(deps))] = link
        if active == previous:
            return {tid: {**topic, 'relations': list(topic['relations'].values())}
                    for tid, topic in selected.items()}
