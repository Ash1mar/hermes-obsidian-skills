"""Versioned page planning, scoped evidence obligations and fragment acceptance."""
import re

from hermes_source_units.semantic_submission import closed
from hermes_source_units.validation import _check_shape, fingerprint
from short_evidence_pass import NAME, NOTE, PASS_LIMIT, REF, array, evidence, request, require, source_revision
from short_evidence_reduce import SECTION, ISSUE, check_coordination as check_partition

CONTRACT = 'short-evidence-pages/v2'
RECEIPT = 'short-evidence-fragment-receipt/v2'
EXPLANATION = {'type': 'string', 'minLength': 1, 'maxLength': 240}
PAGE = closed({
    'name': NAME, 'scope': {**NOTE, 'minLength': 1}, 'topics': array(REF), 'sections': array(SECTION),
    'reader_question': EXPLANATION, 'grouping_reason': EXPLANATION,
    'grouping_basis': {'enum': ['single_topic', 'shared_reader_task', 'inseparable_conditions', 'comparison']},
})
COORDINATION = closed({'pages': array(PAGE), 'unresolved': array(ISSUE)})
SECTION_BODY = closed({
    'heading': NAME, 'content': {'type': 'string', 'minLength': 1}, 'expressed': array(REF),
})
RESULT = {'anyOf': [
    closed({'sections': {**array(SECTION_BODY), 'minItems': 1}, 'context': array(REF),
            'unresolved': array(closed({'item': REF, 'reason': EXPLANATION})),
            'inspected_assets': array(REF)}),
    closed({'blocked': closed({'kind': {'enum': ['missing_asset', 'unreadable', 'missing_context', 'other']},
                              'refs': array(REF), 'detail': EXPLANATION, 'effect': EXPLANATION})}),
]}
RULES = {
    'short-coordinate': (
        'Plan Chinese knowledge pages by independent reader questions and applicability. Assign every topic exactly '
        'once to a page or an unresolved issue. State reader_question, scope, grouping_basis and a specific '
        'grouping_reason. Source identity and similar titles do not justify merging. Keep independently useful '
        'tasks or different applicability on separate pages when appropriate. A coherent shared task can span '
        'several sections. Sections partition page topics or are empty. Input/output sizing is for physical '
        'writing fragments only; size does not determine the knowledge page boundary. In a local issue, reassess '
        'only the named topics and give specific reasons for retaining or revising the grouping.'
    ),
    'short-write': (
        'Write only the assigned fragment in Chinese. Each slot is independent. The page question and '
        'section position are navigation; never write the whole page or another fragment. Follow obligations: '
        'express required substantive evidence, conditions, exceptions and conflicts with their scopes and '
        'limits. Use shared context for correct interpretation and avoid repeating unrelated material. '
        'Return Markdown section content with one inline [S1-B1] style citation per used ID. Each section '
        'lists the exact inline IDs in expressed. Assign every supplied ID to expressed, context or unresolved '
        'once by disposition (an expressed ID may be cited in several sections). Use context for navigation '
        'and supporting material not used in the body. Inspect every required whole asset and list its ID. '
        'Unlocated source QA is a release-review issue; assess actual evidence, and preserve applicable '
        'limitations. If essential evidence fails, return blocked with exact refs, missing or unreadable '
        'content and effect. Do not infer, invent citations, or use absence of unrequired assets as a refusal.'
    ),
}
_INLINE = re.compile(r'\[(S[0-9]+-[AB][0-9]+)\]')
_METADATA = re.compile(r'<!--\s*(?:source-page|table-id|block-id):[^<>]*-->')
_METADATA_TAIL = re.compile(r'^:\s*(?:table|block)_\w+;\s*source-page:\s*\d+;\s*qa:\s*\w+\s*-->\s*$')
_TABLE_LINK = re.compile(r'!?\[[^\]\n]+\]\(([^)\n]+)\)')
_CAPTION = re.compile(r'(?:Table\s+[\w-]+|[\w\u3400-\u9fff\s]{1,24})', re.IGNORECASE)
_OBLIGATION = re.compile(r'必须|不得|不应|须|应当|应|必须|仅|除非|适用|至少|最多|\d|\b(?:shall|must|except|only)\b',
                         re.IGNORECASE)


def navigation_wrapper(item, registered_paths):
    """Recognize a narrow table-wrapper grammar; retain any other text as evidence."""
    if item['kind'] != 'block' or not item['required_assets'] or not item['text']:
        return False
    text = _METADATA.sub('', item['text'])
    links = _TABLE_LINK.findall(text)
    if not links or not set(links) <= registered_paths:
        return False
    for line in _TABLE_LINK.sub('', text).splitlines():
        value = line.strip()
        if not value or _METADATA_TAIL.fullmatch(value):
            continue
        caption = re.fullmatch(r'\*\*([^*]+)\*\*', value)
        if caption and _CAPTION.fullmatch(caption[1]) and (
            re.fullmatch(r'Table\s+[\w-]+', caption[1], re.IGNORECASE) or not _OBLIGATION.search(caption[1])
        ):
            continue
        # Retain organizational cover headings as context along with the actual tables.
        if (re.fullmatch(r'#{1,6} [\u3400-\u9fff]{2,32}公司', value)
                or re.fullmatch(r'[A-Za-z][A-Za-z ,.&]{2,72}\b(?:Co\.,?\s*LTD\.|Corporation)', value, re.IGNORECASE)):
            continue
        return False
    return True


def obligations(sources, material, topics, primary):
    by_item = {}
    for topic in topics.values():
        for link in topic['relations']:
            by_item.setdefault(link['item'], set()).add(link['role'])
    rows = {}
    for key, item in material.items():
        source = sources[key.split('-', 1)[0]]
        roles = by_item.get(key, set())
        covers_by = []
        if item['kind'] == 'asset':
            classification = 'asset'
            required = key in primary and 'asset' in roles
        elif roles & {'evidence', 'condition', 'exception', 'conflict', 'contrary'}:
            classification = 'substantive'
            required = key in primary
        elif 'asset' in roles:
            assets = item['required_assets']
            paths = {source['items'][ref]['locator']['path'] for ref in assets}
            is_wrapper = navigation_wrapper(item, paths)
            classification = 'navigation' if is_wrapper else 'substantive'
            covers_by = list(assets) if is_wrapper and key in primary else []
            required = not is_wrapper and key in primary
        else:
            classification, required = 'context', False
        rows[key] = {'role': classification, 'mapped_roles': sorted(roles), 'express': required,
                     'inspect': item['kind'] == 'asset', 'covers_by': covers_by,
                     'assignment': 'primary' if key in primary else 'shared'}
    return rows


def qa_impacts(sources, selection, material, obligations_by_id):
    rows = []
    for key in selection:
        source = sources[key]
        qa = source['qa']
        entries = [(issue['code'], issue.get('severity') == 'info', issue.get('message', ''))
                   for issue in qa.get('issues', [])]
        entries.extend((code, False, 'registered source review marker') for code in qa.get('review_required', []))
        for code, info, basis in entries:
            rows.append({'id': fingerprint([key, code, basis])[:16], 'source': key, 'type': code,
                         'items': [], 'scope': 'source' if info else 'unknown', 'basis': basis,
                         'status': 'informational' if info else 'unlocated',
                         'writing': 'allow', 'publication': 'allow' if info else 'review'})
    for key, item in material.items():
        for marker in _METADATA.findall(item['text'] or ''):
            if not re.search(r'\bqa:\s*(?:unreadable|missing|fail)\b', marker, re.I):
                continue
            table = re.match(r'<!--\s*table-id:\s*([\w-]+)', marker)
            if table and item['kind'] == 'block':
                affected = [dep for dep in item['required_assets'] if dep in material
                            and material[dep].get('label', '').rsplit('/', 1)[-1].rsplit('.', 1)[0] == table[1]]
            else:
                affected = [key]
            for ref in affected:
                if obligations_by_id[ref]['role'] == 'navigation':
                    continue
                row = {'id': fingerprint([ref, marker])[:16], 'source': ref.split('-', 1)[0],
                       'type': 'converter_material_failure', 'items': [ref], 'scope': material[ref]['kind'],
                       'basis': marker, 'status': 'localized', 'writing': 'hold', 'publication': 'hold'}
                if row not in rows:
                    rows.append(row)
    return rows


def topic_items(topics):
    result = {}
    for topic in topics.values():
        items = result.setdefault(topic['source'], {})
        for relation in topic['relations']:
            for item in (relation['item'], *relation['applies_to']):
                items[item] = None
    return result


def coordination_request(sources, reduced, issue=None):
    topics = reduced['topics'] if issue is None else {key: reduced['topics'][key] for key in issue['topics']}
    if issue is None and len(topics) < 2:
        return None
    flagged, images, catalogue = {}, {}, {}
    for key, topic in topics.items():
        source = sources[topic['source']]
        items, pictures = evidence(source, topic_items({key: topic})[topic['source']])
        visible = [link for link in topic['relations'] if link['role'] in ('conflict', 'contrary')]
        if issue is not None and not visible:
            # Coordination judges reader scope. Supply focused scope anchors and
            # their entire dependencies without importing the oversized page.
            scoped = [link for link in topic['relations'] if link['role'] in ('condition', 'exception', 'definition')]
            visible = (scoped or topic['relations'])[:1]
        if visible:
            focus = dict.fromkeys(item for link in visible for item in (link['item'], *link['applies_to']))
            originals, attachments = evidence(source, focus)
            flagged.update(originals)
            images.update({fingerprint([pic['sha256'], pic['references']]): pic for pic in attachments})
        catalogue[key] = {
            'name': topic['name'], 'source': topic['source'], 'scope': topic['scope'],
            'materials': len(items), 'assets': sum(item['kind'] == 'asset' for item in items.values()),
            'material_characters': sum(len(item['text'] or '') for item in items.values()),
            'asset_bytes': sum(p['bytes'] for p in pictures),
        }
    view = {'page_contract': CONTRACT, 'map_sha256': fingerprint(reduced),
            'revisions': {key: source_revision(src) for key, src in sources.items()},
            'topics': catalogue, 'flagged_evidence': flagged}
    if issue is not None:
        view['issue'] = issue
    else:
        view['unresolved_materials'] = reduced['unresolved']
    return request('short-coordinate', view, COORDINATION, 720 + 166 * len(topics), PASS_LIMIT, images.values())


def check_coordination(view, answer):
    check_partition(view, answer)
    for page in answer['pages']:
        require(page['reader_question'].strip() and page['grouping_reason'].strip(),
                'reader question and grouping reason are required')
        require((len(page['topics']) == 1) == (page['grouping_basis'] == 'single_topic'),
                'grouping basis must match the page membership')


def coordination_issues(answer, topics):
    """Use a targeted second look for conspicuously broad undifferentiated groups."""
    issues = list(answer['unresolved'])
    for page in answer['pages']:
        partitions = page['sections'] or [{'topics': page['topics']}]
        largest = max(partitions, key=lambda section: len(section['topics']))['topics']
        if len(largest) >= 8 and len({topics[key]['scope'] for key in largest}) >= 4:
            issues.append({'topics': page['topics'],
                           'reason': 'One section groups many distinct applicability scopes. Reassess the reader task.'})
    return issues


def body_request(group, limit, *, repair_contract=None):
    require(1 <= len(group) <= 2 and (len(group) == 1 or not any(p['images'] for p in group)),
            'fragment request requires independently transportable slots')
    slots = {f'p{i}': p['view'] for i, p in enumerate(group, 1)}
    estimate = sum(900 + sum(len(m['text'] or '') for m in p['materials'].values()) // 2
                   + 20 * len(p['materials']) for p in slots.values())
    view = {'page_contract': CONTRACT, 'pages': slots}
    if repair_contract:
        view['fragment_repair'] = repair_contract
    return request('short-write', view, dict.fromkeys(slots, RESULT),
                   estimate, limit, [image for p in group for image in p['images']], slots=True)


def difference(label, values):
    return f"{label}={','.join(sorted(values)[:24])}" if values else ''


def check_fragment(row, slot, answer):
    require(row['budget']['fits'], 'fragment request exceeds its budget')
    _check_shape(RESULT, answer, {}, '$')
    view = row['view']['pages'][slot]
    duties = view['obligations']
    known = set(duties)
    state, diagnostic = 'structurally_valid', None
    qa_observations = []
    block_locations = None
    if 'blocked' in answer:
        block = answer['blocked']
        scoped = 'material_locations_sha256' in view
        require((scoped or set(block['refs']) <= known) and block['detail'].strip() and block['effect'].strip(),
                'refusal must cite supplied identifiers and describe its concrete effect')
        if scoped:
            from short_fragment_disposition import verify_block
            block_locations = verify_block(row, view, block['refs'])
        scoped_assets = {key for key, duty in duties.items() if duty['inspect']}
        refs = set(block['refs'])
        if block['kind'] == 'missing_asset':
            refs |= {dep for item in refs & known for dep in view['materials'][item]['required_assets']}
            diagnostic = 'asset_claim_requires_verification' if refs & scoped_assets else 'no_named_required_asset'
        else:
            diagnostic = 'reason_requires_verification' if refs else 'refusal_has_no_scoped_reference'
        state = 'held'
    else:
        expressed = set()
        for section in answer['sections']:
            require(section['heading'].strip() and section['content'].strip(), 'section needs a heading and content')
            inline = set(_INLINE.findall(section['content']))
            declared = set(section['expressed'])
            require(inline == declared and inline <= known, '; '.join(filter(None, (
                difference('declared_without_inline', declared - inline), difference('inline_without_declaration', inline - declared),
                difference('unknown_inline', inline - known),
            ))))
            expressed |= declared
        context = set(answer['context'])
        unresolved = {entry['item'] for entry in answer['unresolved']}
        inspected = set(answer['inspected_assets'])
        require(len(unresolved) == len(answer['unresolved']) and all(x['reason'].strip() for x in answer['unresolved']),
                'each unresolved item needs one reason')
        require(not (expressed & context or expressed & unresolved or context & unresolved), 'overlapping evidence dispositions')
        # Known nonblocking QA identifiers may be mentioned as observations.
        # They are never evidence, cannot cover a source obligation, and do
        # not resolve the preserved publication review.
        qa_ids = {q['id'] for q in view['qa_impact'] if q['writing'] == 'allow'}
        qa_observations = [
            {'qa_id': entry['item'], 'status': 'unresolved', 'reason': entry['reason']}
            for entry in answer['unresolved'] if entry['item'] in qa_ids
        ]
        qa_observations.extend({'qa_id': key, 'status': 'context'} for key in answer['context'] if key in qa_ids)
        unresolved -= qa_ids
        context -= qa_ids
        accounted = expressed | context | unresolved
        required = {key for key, duty in duties.items() if duty['express']}
        assets = {key for key, duty in duties.items() if duty['inspect']}
        substitute = {dep for key, duty in duties.items() if key in context for dep in duty['covers_by']}
        errors = [difference('unaccounted', known - accounted), difference('unknown_ids', accounted - known),
                  difference('required_expressions_missing', (required | substitute) - expressed - unresolved),
                  difference('asset_inspections_missing', assets - inspected - unresolved),
                  difference('unexpected_asset_inspections', inspected - assets)]
        require(not any(errors), '; '.join(filter(None, errors)))
        if unresolved & (required | assets | substitute):
            state, diagnostic = 'held', 'unresolved_required_evidence'
    release = [qa['id'] for qa in view['qa_impact'] if qa['publication'] != 'allow']
    body = {'contract': RECEIPT, 'request_sha256': row['id'], 'slot': slot, 'fragment_id': view['fragment_id'],
            'page_id': view['id'], 'fragment_position': view['fragment_position'], 'fragment_sha256': fingerprint(view),
            'answer': answer, 'state': state, 'diagnostic': diagnostic, 'qa_publication_review': release,
            'independent_review': 'not_performed', 'publication_eligible': False}
    if qa_observations:
        body.update(contract='short-evidence-fragment-receipt/v2.1', qa_observations=qa_observations)
    if block_locations is not None:
        body.update(contract='short-evidence-fragment-receipt/v2.2', blocked_material_locations=block_locations)
    return {**body, 'receipt_sha256': fingerprint(body)}
