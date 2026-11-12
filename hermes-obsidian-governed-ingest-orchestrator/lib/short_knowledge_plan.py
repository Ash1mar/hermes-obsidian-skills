"""Independent page ownership and bounded reuse of mapped evidence in syntheses."""
import unicodedata

from hermes_source_units.validation import fingerprint

CONTRACT = 'short-knowledge-plan/v1'
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


def syntheses(reduced, proposed, entries, *, namespace=None, entry_id):
    """Keep failed proposals local and leave the primary coverage partition intact."""
    topics = reduced['topics']
    owners = {key: page for page in entries for key in page['topics']}
    specs, gaps, seen = [], [], set()
    for row in proposed:
        anchors = row['anchors']
        pairs = {(a['topic'], a['ref']) for a in anchors}
        members = sorted({a['topic'] for a in anchors})
        if (not all(row[field].strip() for field in ('name', 'scope', 'reader_question', 'theme_directory'))
                or not 2 <= len(anchors) <= MAX_ANCHORS or len(pairs) != len(anchors)
                or len({a['ref'] for a in anchors}) < 2):
            reason = 'synthesis_requires_distinct_bounded_anchors_and_scope'
        elif any(key not in owners or owners[key]['page_type'] not in ENTRY_TYPES
                 or ref not in anchor_options(topics[key]) for key, ref in pairs):
            reason = 'synthesis_anchor_lacks_independent_owner_or_mapped_evidence'
        else:
            related = {entry_id(owners[key]): owners[key] for key in members}
            spec = {**{key: row[key] for key in ('name', 'scope', 'reader_question', 'theme_directory')},
                    'page_type': 'topic', 'topics': members, 'sections': [],
                    'grouping_basis': 'shared_reader_task', 'grouping_reason': 'A scoped synthesis of independent pages.',
                    'synthesis_basis': {'anchors': sorted(anchors, key=lambda a: (a['topic'], a['ref'])),
                                        'entries': [{'short_id': key, **{field: related[key][field]
                                                     for field in ('name', 'scope', 'page_type')}}
                                                    for key in sorted(related)]}}
            key = synthesis_id(reduced, spec, namespace)
            if len(related) < 2:
                reason = 'synthesis_requires_two_independent_pages'
            elif key in seen:
                reason = 'duplicate_synthesis_membership'
            else:
                seen.add(key)
                specs.append(spec)
                continue
        gaps.append({'name': row['name'], 'topics': members, 'anchors': anchors, 'reason': reason})
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
