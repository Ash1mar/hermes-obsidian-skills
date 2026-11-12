"""Versioned page planning, scoped evidence obligations and fragment acceptance."""
import re

from hermes_source_units import ContractError
from hermes_source_units.semantic_submission import closed
from hermes_source_units.short_build import PAGE_TYPES, TYPED_LAYOUT
from hermes_source_units.validation import _check_shape, fingerprint
from short_evidence_pass import NAME, NOTE, PASS_LIMIT, REF, array, evidence, request, require, source_revision
from short_evidence_reduce import SECTION, ISSUE, check_coordination as check_partition

CONTRACT = 'short-evidence-pages/v2'
WRITE_CONTRACT = 'short-evidence-writing/v3'
RECEIPT = 'short-evidence-fragment-receipt/v2'
EXPLANATION = {'type': 'string', 'minLength': 1, 'maxLength': 240}
PAGE = closed({
    'name': NAME, 'scope': {**NOTE, 'minLength': 1}, 'topics': array(REF), 'sections': array(SECTION),
    'reader_question': EXPLANATION, 'grouping_reason': EXPLANATION,
    'grouping_basis': {'enum': ['single_topic', 'shared_reader_task', 'inseparable_conditions', 'comparison']},
})
COORDINATION = closed({'pages': array(PAGE), 'unresolved': array(ISSUE)})
CARD_COORDINATION = closed({**COORDINATION['properties'], 'pages': array(closed({
    **PAGE['properties'], 'theme_directory': NAME,
}))})
TYPED_COORDINATION = closed({**COORDINATION['properties'], 'pages': array(closed({
    **PAGE['properties'], 'theme_directory': NAME, 'page_type': {'enum': list(PAGE_TYPES)},
    'project_basis': {'anyOf': [closed({'name': NAME, 'refs': {**array(REF), 'minItems': 1}}),
                                {'type': 'null'}]},
}))})
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
WRITING_RESULT = {'anyOf': [
    closed({**RESULT['anyOf'][0]['properties'],
            'sections': {**array(closed({'heading': NAME, 'content': SECTION_BODY['properties']['content']})),
                         'minItems': 1}}),
    RESULT['anyOf'][1],
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
WRITING_RULE = (
    'Write only the assigned Chinese fragment and only for its own slot. Follow the evidence obligations, '
    'including conditions, exceptions, numerical limits and applicability. Cite every used source ID inline '
    'in Markdown using [S1-B1]. The program derives used IDs from the Markdown. List unused context and '
    'genuinely unresolved material; do not report a cited item as unresolved. Explicitly list each whole '
    'asset inspected. A pure table navigation block can be accounted for by its bound complete tables '
    'when those tables are inspected and cited. QA annotations are retained as metadata and do not '
    'require publication review or justify refusal. A concrete missing or unreadable essential item '
    'must still be reported with its exact ID and impact. Keep source conditions and exceptions intact.'
)
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


def table_navigation(item, materials):
    """Return only registered assets named by a pure table wrapper."""
    if item['kind'] != 'block' or not item['text']:
        return []
    bindings = {materials[ref].get('label'): ref for ref in item['required_assets']
                if ref in materials and materials[ref]['kind'] == 'asset'}
    text = _METADATA.sub('', item['text'])
    links = _TABLE_LINK.findall(text)
    if not links or not set(links) <= bindings.keys():
        return []
    if navigation_wrapper(item, set(bindings)):
        return list(dict.fromkeys(bindings[link] for link in links))
    caption = re.compile(r'\*\*(?P<title>[\w\u3400-\u9fff ]{2,40}?)(?:\s*编号[：:]\s*(?:[A-Z0-9-]+)?\s*)?'
                         r'\s*共\s*\d*\s*页\s*第\s*\d*\s*页\*\*')
    titles = {m['title'].strip() for line in text.splitlines()
              if (m := caption.fullmatch(line.strip())) and not _OBLIGATION.search(m['title'])}
    organization = r'[\u3400-\u9fff]{2,32}公司'
    continuation = r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}\s*[（(]续[）)]'
    for line in text.splitlines():
        value = line.strip()
        title = re.sub(r'^#{1,6}\s+', '', value)
        if (not value or _TABLE_LINK.fullmatch(value)
                or ((m := caption.fullmatch(value)) and m['title'].strip() in titles)
                or (not _OBLIGATION.search(title) and any(
                    re.fullmatch(r'[\u3400-\u9fff]{0,8}' + re.escape(t), title) for t in titles))
                or re.fullmatch(rf'(?:{organization}|{continuation})', title)):
            continue
        return []
    return list(dict.fromkeys(bindings[link] for link in links))


def writing_view(view):
    """Keep unlocated QA as annotations, separate from material controls."""
    view['writing_contract'] = WRITE_CONTRACT
    impacts = view['qa_impact']
    view['qa_annotations'] = [{k: v for k, v in q.items() if k not in ('publication', 'writing')}
                              for q in impacts if q['writing'] == 'allow']
    view['qa_impact'] = [q for q in impacts if q['writing'] != 'allow']
    view.pop('original_qa', None)
    return view


def local_task(view, bindings):
    """Project the writing question from the slot's own topics and evidence."""
    view.pop('reader_question', None)
    view['name'] = view['fragment_position']['name']
    scopes = list(dict.fromkeys(topic['scope'] for topic in view['topics'].values()))
    view['scope'] = '；'.join(scopes)
    view['fragment_task'] = {
        'question': f"仅就「{view['name']}」片段，所分配的原文给出了哪些要求和依据？",
        'deliverable_scope': scopes,
        'required_evidence': [key for key, duty in view['obligations'].items() if duty['express']],
        'preserve_limitations': [key for key, duty in view['obligations'].items()
                                 if set(duty['mapped_roles']) & {'condition', 'exception', 'conflict', 'contrary'}],
        'asset_bindings': bindings,
    }


def auditable_qa(view):
    return [*view['qa_impact'], *[{**q, 'writing': 'allow', 'publication': 'review'}
            for q in view.get('qa_annotations', ()) if q['status'] == 'unlocated']]


def obligations(sources, material, topics, primary, *, modern=False, card=False):
    substantive = {'evidence', 'condition', 'exception', 'conflict', 'contrary'}
    if card:
        substantive.add('definition')
    protected_roles = substantive - {'evidence'}
    by_item = {}
    for topic in topics.values():
        for link in topic['relations']:
            by_item.setdefault(link['item'], set()).add(link['role'])
    rows = {}
    for key, item in material.items():
        source = sources[key.split('-', 1)[0]]
        roles = by_item.get(key, set())
        covers_by = []
        navigation = table_navigation(item, material) if modern and not roles & protected_roles else []
        if item['kind'] == 'asset':
            classification = 'asset'
            required = key in primary and bool(roles)
        elif navigation:
            classification, required = 'navigation', False
            covers_by = navigation
        elif roles & substantive:
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
    layout = reduced.get('card_layout')
    typed = bool(layout and layout['contract'] == TYPED_LAYOUT)
    if issue is None and (not topics or (len(topics) < 2 and not typed)):
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
        if typed:
            catalogue[key]['project_text_refs'] = sorted({
                link['item'] for link in topic['relations'] if link['role'] in ('evidence', 'definition')
                and items[link['item']]['kind'] == 'block' and items[link['item']]['text']
            })
    view = {'page_contract': CONTRACT, 'map_sha256': fingerprint(reduced),
            'revisions': {key: source_revision(src) for key, src in sources.items()},
            'topics': catalogue, 'flagged_evidence': flagged}
    if issue is not None:
        view['issue'] = issue
    else:
        view['unresolved_materials'] = reduced['unresolved']
    if layout:
        view['existing_theme_directories'] = layout['themes']
    if typed:
        view['typed_pages'] = True
    schema = TYPED_COORDINATION if typed else CARD_COORDINATION if layout else COORDINATION
    return request('short-coordinate', view, schema,
                   720 + (215 if typed else 190 if layout else 166) * len(topics), PASS_LIMIT, images.values())


def check_coordination(view, answer):
    check_partition(view, answer)
    for page in answer['pages']:
        require(page['reader_question'].strip() and page['grouping_reason'].strip(),
                'reader question and grouping reason are required')
        require((len(page['topics']) == 1) == (page['grouping_basis'] == 'single_topic'),
                'grouping basis must match the page membership')
        if 'existing_theme_directories' in view:
            require(page['theme_directory'].strip(), 'a shared theme directory is required')
        if view.get('typed_pages'):
            require(page['page_type'] == 'project' or page['project_basis'] is None,
                    'only a Project can declare project basis')


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


def writing_output(page):
    materials = page['materials']
    assigned = page.get('writing_assignment', {}).get('primary', materials)
    return 900 + sum(len(materials[key]['text'] or '') for key in assigned) // 2 + 20 * len(materials)


def body_request(group, limit, *, repair_contract=None):
    require(1 <= len(group) <= 2 and (len(group) == 1 or not any(p['images'] for p in group)),
            'fragment request requires independently transportable slots')
    slots = {f'p{i}': p['view'] for i, p in enumerate(group, 1)}
    estimate = sum(writing_output(p) for p in slots.values())
    view = {'page_contract': CONTRACT, 'pages': slots}
    modern = all(p.get('writing_contract') == WRITE_CONTRACT for p in slots.values())
    if modern:
        view['writing_contract'] = WRITE_CONTRACT
    if any(p.get('card_contract') for p in slots.values()):
        view['card_content'] = True
    if any(p.get('fragment_task') for p in slots.values()) and not repair_contract:
        view['local_fragment_scope'] = True
    if repair_contract:
        view['fragment_repair'] = repair_contract
    return request('short-write', view, dict.fromkeys(slots, WRITING_RESULT if modern else RESULT),
                   estimate, limit, [image for p in group for image in p['images']], slots=True)


def difference(label, values, *, limit=24):
    return f"{label}={','.join(sorted(values)[:limit])}" if values else ''


def section_citations(section):
    return set(_INLINE.findall(section['content']))


def inspection_gaps(row, slot, draft):
    """An inspection patch is eligible only if adding those attestations is sufficient."""
    if not isinstance(draft, dict) or 'sections' not in draft or not isinstance(draft.get('inspected_assets'), list):
        return []
    try:
        _check_shape(WRITING_RESULT, draft, {}, '$')
        duties = row['view']['pages'][slot]['obligations']
        missing = sorted({key for key, duty in duties.items() if duty['inspect']} - set(draft['inspected_assets']))
        patched = {**draft, 'inspected_assets': [*draft['inspected_assets'], *missing]}
        if missing and check_fragment(row, slot, patched)['state'] == 'structurally_valid':
            return missing
    except (ContractError, ValueError):
        pass
    return []


def writing_retry(row, slots, drafts, diagnostics, *, inspection_patch=False):
    properties = row['schema']['properties']['results']['properties']
    pages = {slot: row['view']['pages'][slot] for slot in slots}
    retry = {slot: {'previous_answer': drafts.get(slot), 'diagnostics': diagnostics[slot]} for slot in slots}
    schemas = {slot: properties[slot] for slot in slots}
    estimate = 0
    for slot, page in pages.items():
        missing = inspection_gaps(row, slot, drafts.get(slot)) if inspection_patch else []
        if missing:
            retry[slot] = {'locked_draft_sha256': fingerprint(drafts[slot]), 'missing_inspections': missing,
                           'diagnostics': diagnostics[slot]}
            pages[slot] = {'materials': {key: page['materials'][key] for key in missing}}
            schemas[slot] = closed({'inspected_assets': array({'enum': missing})})
            estimate += 160 + 20 * len(missing)
        else:
            estimate += writing_output(page)
    materials = {key for page in pages.values() for key in page['materials']}
    images = [image for image in row['image_inputs']
              if any(ref['material'] in materials for ref in image['references'])]
    view = {**row['view'], 'pages': pages, 'structural_retry': {'slots': retry}}
    return request('short-write', view, schemas, estimate, row['budget']['input_cap'], images, slots=True)


def retry_slots(call):
    return call['view'].get('structural_retry', {}).get('slots', {})


def restore_locked_draft(call, slot, answer, draft):
    patch = retry_slots(call).get(slot, {})
    if 'locked_draft_sha256' not in patch:
        return answer
    expected = patch['missing_inspections']
    require(fingerprint(draft) == patch['locked_draft_sha256'], 'locked writing draft changed')
    require(isinstance(answer, dict) and set(answer) == {'inspected_assets'}
            and isinstance(answer['inspected_assets'], list)
            and all(isinstance(ref, str) for ref in answer['inspected_assets'])
            and sorted(answer['inspected_assets']) == expected,
            'inspection patch requires exactly the supplied asset attestations')
    return {**draft, 'inspected_assets': [*draft['inspected_assets'], *expected]}


def check_fragment(row, slot, answer):
    require(row['budget']['fits'], 'fragment request exceeds its budget')
    view = row['view']['pages'][slot]
    modern = view.get('writing_contract') == WRITE_CONTRACT
    _check_shape(WRITING_RESULT if modern else RESULT, answer, {}, '$')
    if view.get('card_contract') and 'sections' in answer:
        from short_cards import verify_fragment
        verify_fragment(answer['sections'])
    duties = view['obligations']
    known = set(duties)
    if view.get('card_contract') and 'sections' in answer:
        cited = set().union(*(section_citations(section) for section in answer['sections']))
        accounted = cited | set(answer['context'])
        # A duplicate checkmark on accounted text cannot establish asset inspection.
        # Preserve unknown IDs, real asset attestations and unresolved content.
        redundant = {key for key in answer['inspected_assets'] if key in accounted
                     and view['materials'].get(key, {}).get('kind') == 'block' and not duties[key]['inspect']}
        if redundant:
            answer = {**answer, 'inspected_assets': [key for key in answer['inspected_assets'] if key not in redundant]}
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
        errors = []
        for section in answer['sections']:
            require(section['heading'].strip() and section['content'].strip(), 'section needs a heading and content')
            inline = section_citations(section)
            declared = inline if modern else set(section['expressed'])
            problems = (difference('declared_without_inline', declared - inline),
                        difference('inline_without_declaration', inline - declared),
                        difference('unknown_inline', inline - known, limit=None if modern else 24))
            if modern:
                errors.extend(problems)
            else:
                require(inline == declared and inline <= known, '; '.join(filter(None, problems)))
            expressed |= declared
        context = set(answer['context'])
        unresolved = {entry['item'] for entry in answer['unresolved']}
        inspected = set(answer['inspected_assets'])
        require(len(unresolved) == len(answer['unresolved']) and all(x['reason'].strip() for x in answer['unresolved']),
                'each unresolved item needs one reason')
        if modern:
            context -= expressed
            errors.append(difference('overlapping_unresolved', unresolved & (expressed | context), limit=None))
        else:
            require(not (expressed & context or expressed & unresolved or context & unresolved), 'overlapping evidence dispositions')
        # Known nonblocking QA identifiers may be mentioned as observations.
        # They are never evidence, cannot cover a source obligation, and do
        # do not resolve the preserved QA annotation.
        qa_ids = {q['id'] for q in auditable_qa(view) if q['writing'] == 'allow'}
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
        if modern:
            covered_nav = {key for key, duty in duties.items() if duty['covers_by']
                           and set(duty['covers_by']) <= expressed & inspected}
            accounted |= covered_nav | inspected
            substitute |= {ref for duty in duties.values()
                           if duty['assignment'] == 'primary' for ref in duty['covers_by']}
        cap = None if modern else 24
        errors += [difference('unaccounted', known - accounted, limit=cap),
                   difference('unknown_ids', accounted - known, limit=cap),
                   difference('required_expressions_missing', (required | substitute) - expressed - unresolved, limit=cap),
                   difference('asset_inspections_missing', assets - inspected - unresolved, limit=cap),
                   difference('unexpected_asset_inspections', inspected - assets, limit=cap)]
        require(not any(errors), '; '.join(filter(None, errors)))
        if unresolved & (required | assets | substitute):
            state, diagnostic = 'held', 'unresolved_required_evidence'
    release = [qa['id'] for qa in auditable_qa(view) if qa['publication'] != 'allow']
    body = {'contract': 'short-evidence-fragment-receipt/v3' if modern else RECEIPT,
            'request_sha256': row['id'], 'slot': slot, 'fragment_id': view['fragment_id'],
            'page_id': view['id'], 'fragment_position': view['fragment_position'], 'fragment_sha256': fingerprint(view),
            'answer': answer, 'state': state, 'diagnostic': diagnostic, 'qa_publication_review': release,
            'independent_review': 'not_performed', 'publication_eligible': False}
    if qa_observations:
        body['qa_observations'] = qa_observations
        if not modern:
            body['contract'] = 'short-evidence-fragment-receipt/v2.1'
    if block_locations is not None:
        body['blocked_material_locations'] = block_locations
        if not modern:
            body['contract'] = 'short-evidence-fragment-receipt/v2.2'
    return {**body, 'receipt_sha256': fingerprint(body)}
