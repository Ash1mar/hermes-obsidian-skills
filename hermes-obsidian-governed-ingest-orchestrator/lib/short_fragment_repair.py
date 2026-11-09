"""Pinned, slot-local writing recovery and exact same-page coverage reuse."""
import copy
import re

from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import fingerprint
from short_evidence_pass import require
from short_page_contract import body_request, check_fragment, navigation_wrapper

CONTRACT = 'short-fragment-repair/v1'
_ORG = r'[\u3400-\u9fff]{2,32}公司'
_CODE = r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}'
_CONTINUATION = rf'{_CODE}\s*[（(]续[）)]'
_APPENDIX = rf'附录\s*\d+\s*[：:]\s*{_CODE}\s+[\u3400-\u9fff]{{2,24}}'
_FORM = r'(?:[\u3400-\u9fff]{2,32}公司\s+)?[\u3400-\u9fff]{2,24}(?:\s*[（(]续页[）)])?\s+编号[：:]\s*共\s*\d*\s*页\s*第\s*\d*\s*页'
_NORMATIVE = re.compile(r'必须|不得|应|需|方可|允许|禁止|仅限|适用|须|至少|最多|除非|\b(?:must|shall|only)\b',
                        re.IGNORECASE)


def form_navigation(item, registered_paths):
    """Recognize structural form headings only when whole registered links remain."""
    if not item['text'] or not item['required_assets']:
        return False
    lines = []
    for line in item['text'].splitlines():
        value = line.strip()
        heading = re.sub(r'^#{1,6}\s+', '', value)
        caption = re.fullmatch(r'\*\*([^*]+)\*\*', value)
        boilerplate = re.fullmatch(r'(?:source-)?page:\s*\d+\s*-->', value)
        known_title = re.fullmatch(rf'(?:{_ORG}|{_CONTINUATION}|{_APPENDIX})', heading)
        known_caption = caption and re.fullmatch(_FORM, caption[1])
        if boilerplate or ((known_title or known_caption) and not _NORMATIVE.search(value)):
            continue
        lines.append(line)
    return navigation_wrapper({**item, 'text': '\n'.join(lines)}, registered_paths)


def scoped_request(row, slot, limit, *, locations=None, continuations=()):
    """Build a single slot with explicit local authority and source-bound form references."""
    old = row['view']['pages'][slot]
    view = copy.deepcopy(old)
    position = view['fragment_position']
    materials = view['materials']
    bindings = {}
    for key, item in materials.items():
        linked = item['required_assets']
        if not linked:
            continue
        require(all(ref in materials and materials[ref]['kind'] == 'asset'
                    and ref.split('-', 1)[0] == key.split('-', 1)[0] for ref in linked),
                'repair lost a whole same-source registered asset')
        assets = {ref: materials[ref]['label'] for ref in linked}
        duty = view['obligations'][key]
        if set(duty['mapped_roles']) <= {'asset', 'context', 'definition'} and form_navigation(item, set(assets.values())):
            duty.update(role='navigation', express=False, covers_by=linked if duty['assignment'] == 'primary' else [])
        bindings[key] = assets
    view.pop('reader_question', None)
    view['name'] = position['name']
    scopes = list(dict.fromkeys(topic['scope'] for topic in view['topics'].values()))
    view['scope'] = '；'.join(scopes)
    view['fragment_task'] = {
        'question': f"仅就「{position['name']}」片段，所分配的原文给出了哪些要求和依据？",
        'deliverable_scope': scopes,
        'required_evidence': [key for key, duty in view['obligations'].items() if duty['express']],
        'preserve_limitations': [key for key, duty in view['obligations'].items()
                                 if set(duty['mapped_roles']) & {'condition', 'exception', 'conflict', 'contrary'}],
        'asset_bindings': bindings,
    }
    if locations is not None:
        view['source_continuations'] = list(continuations)
        view['material_locations_sha256'] = fingerprint(locations)
    view['repair_of_fragment'] = view.pop('fragment_id')
    view['fragment_id'] = fingerprint([CONTRACT, view])
    images = [img for img in row['image_inputs']
              if any(ref['material'] in materials for ref in img['references'])]
    replacement = body_request([{'view': view, 'images': images}], limit, repair_contract=CONTRACT)
    if locations is not None:
        replacement['material_locations'] = locations
    return replacement


def saved_fragment(saved, row, slot):
    path = saved / f"{row['id']}-{slot}.json"
    if not path.exists():
        return None
    result = _load_json(path)
    require(check_fragment(row, slot, result['answer']) == result, 'fragment receipt changed')
    return result


def equivalent_coverage(target, donor, receipt):
    """Prove exact material and relationship coverage in an accepted same-page peer."""
    if not receipt or receipt['state'] != 'structurally_valid' or target['id'] != donor['id']:
        return None
    if any(donor['source_revisions'].get(key) != value for key, value in target['source_revisions'].items()):
        return None
    if any(donor['materials'].get(key) != item for key, item in target['materials'].items()):
        return None
    if not {fingerprint(qa) for qa in target['qa_impact']} <= {fingerprint(qa) for qa in donor['qa_impact']}:
        return None
    sections = [set(section['expressed']) for section in receipt['answer']['sections']]
    expressed = set().union(*sections)
    inspected = set(receipt['answer']['inspected_assets'])
    required = {key for key, duty in target['obligations'].items() if duty['express']}
    assets = {key for key, duty in target['obligations'].items() if duty['inspect']}
    substitutes = {ref for duty in target['obligations'].values() for ref in duty['covers_by']}
    edges = {(row['item'], ref) for topic in target['topics'].values()
             for row in topic['relations'] for ref in row['applies_to']}
    if not (required | substitutes) <= expressed or not assets <= inspected:
        return None
    if any(not any({item, ref} <= section for section in sections) for item, ref in edges):
        return None
    body = {
        'contract': CONTRACT, 'page_id': target['id'], 'fragment_id': target['fragment_id'],
        'fragment_sha256': fingerprint(target), 'donor_fragment_id': donor['fragment_id'],
        'donor_receipt_sha256': receipt['receipt_sha256'], 'expressed': sorted(required | substitutes),
        'inspected_assets': sorted(assets), 'edges': [list(edge) for edge in sorted(edges)],
        'qa_publication_review': receipt['qa_publication_review'], 'state': 'covered_by_verified_peer',
    }
    return {**body, 'coverage_sha256': fingerprint(body)}


def apply(planned, expected, saved, repair, limit, *, sources=None, excluded=None):
    """Transform only selected fragment identities, keeping old rows and audit readable."""
    require(set(repair) == {'contract', 'expected_pages_sha256', 'changes'}
            and repair['contract'] == CONTRACT and repair['expected_pages_sha256'] == fingerprint(expected),
            'fragment repair does not match the exact writing plan')
    by_id = {v['fragment_id']: (row, slot, v) for row in planned for slot, v in row['view']['pages'].items()}
    expected = copy.deepcopy(expected)
    excluded = excluded or {}
    changed, added, covered, holds, consumed = set(), [], [], [], set()
    for change in repair['changes']:
        require(set(change) == {'fragment_id', 'action', 'prior_receipt_sha256', 'donor_fragment_id'},
                'invalid fragment repair scope')
        fid = change['fragment_id']
        require(fid in by_id and fid not in changed, 'unknown or repeated repair fragment')
        row, slot, view = by_id[fid]
        prior = saved_fragment(saved, row, slot)
        action = change['action']
        require(prior is None or prior['state'] == 'held' or (action == 'context' and fid in excluded),
                'accepted fragment requires an explicit exclusion before repair')
        require((prior['receipt_sha256'] if prior else None) == change['prior_receipt_sha256'],
                'prior fragment receipt changed')
        changed.add(fid)
        consumed.add(row['id'])
        if action == 'isolate':
            require(change['donor_fragment_id'] is None, 'isolated fragment cannot declare a donor')
            holds.append({'page': view['id'], 'fragment_id': fid, 'request': row['id'], 'slot': slot,
                          'reason': 'isolated_prior_substantive_omission'})
            continue
        if action == 'equivalent':
            if change['donor_fragment_id'] in excluded:
                holds.append({'page': view['id'], 'fragment_id': fid, 'request': row['id'], 'slot': slot,
                              'reason': 'coverage_donor_excluded', 'donor': change['donor_fragment_id']})
                continue
            donor_row, donor_slot, donor = by_id[change['donor_fragment_id']]
            proof = equivalent_coverage(view, donor, saved_fragment(saved, donor_row, donor_slot))
            if proof:
                covered.append(proof)
                continue
        else:
            require(action in ('scope', 'context') and change['donor_fragment_id'] is None,
                    'unknown fragment repair action')
        if action == 'context':
            from short_fragment_disposition import context_for, locations
            index = locations(sources, planned, excluded)
            relations = context_for(view, sources, index)
            if not relations or any(rel['status'] != 'resolved' for rel in relations):
                holds.append({'page': view['id'], 'fragment_id': fid, 'reason': 'section_continuation_unresolved',
                              'request': row['id'], 'relations': relations})
                continue
            replacement = scoped_request(row, slot, limit, locations=index, continuations=relations)
        else:
            replacement = scoped_request(row, slot, limit)
        new_id = replacement['view']['pages']['p1']['fragment_id']
        members = expected[view['id']]['fragments']
        members[members.index(fid)] = new_id
        if replacement['budget']['fits']:
            added.append(replacement)
        else:
            holds.append({'page': view['id'], 'fragment_id': new_id, 'request': replacement['id'],
                          'reason': 'repaired_fragment_over_budget', 'budget': replacement['budget']})
    require(changed, 'repair must select at least one failed fragment')
    return added, changed, covered, holds, expected, sorted(consumed)
