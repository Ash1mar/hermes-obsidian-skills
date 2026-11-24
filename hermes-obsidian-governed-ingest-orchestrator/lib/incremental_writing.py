"""Bound native requests and assemble exactly one incremental page revision."""
from collections import defaultdict
from itertools import groupby
import re
from types import SimpleNamespace

from fixed_semantic_media import image_catalog, image_budget, select_images
from hermes_source_units.incremental_pages import (
    preparation_fingerprint, require, unique_refs, validate_page_revision, validate_parent,
)
from hermes_source_units.source_units import _load_json, _sha, _vault_path
from hermes_source_units.validation import ContractError, canonical_json, fingerprint
from incremental_writing_evidence import CURRENT_WRITING_REVISION, EvidenceCatalogue
from short_evidence_pass import request as semantic_request
from short_page_contract import writing_output
from short_page_fragments import connected_groups

PHASE = 'incremental-page-fragment'
CONTRACT = 'incremental-writing-fragments/v1'
INPUT_CAP = 40_000
HANDLE = re.compile(r'\[(m[0-9]+)\]')
PROPERTIES = {'content': {'type': 'string'}, 'inspected': {'type': 'array', 'items': {'type': 'string'}},
              'unresolved': {'type': 'array', 'items': {'type': 'string'}}}
SCHEMA = {'type': 'object', 'properties': PROPERTIES, 'required': list(PROPERTIES), 'additionalProperties': False}
WRITING_REQUIREMENTS = (
    'Use headings and navigation_headings to orient the reader. A structural heading supplies no definition, '
    'parameter, condition or exception by itself. Put each material handle in its own ASCII square brackets '
    'immediately after the statement it supports. Write "…[m1]" for one source or "…[m1][m2]" when both '
    'sources support the same statement. Cite separate statements where each is made. Use only handles '
    'from this request. Forms such as "（m1）", "〔m1〕", "[来源：m1]" and "（m1、m2）" cannot be bound. '
    'Express and cite every assigned substantive material. Record each required original and asset you read '
    'in inspected. Also cite the table at the point where you use its data. '
    'For each unresolved entry, identify the assigned statement affected, the exact missing, clipped or '
    'unreadable clause, handle, table, figure or footnote, and what therefore cannot be determined. '
    'For example, if an assigned maximum storage height ends with "must not exceed" and the value and unit '
    'are cut off, identify that specific limit and keep it unresolved. Write the other supported content. '
    'Return an empty unresolved list when the assigned content has complete support. An omitted-context count '
    'or context_truncated flag alone, or a nearby table that the assigned content does not depend on, '
    'does not make the fragment unresolved. If Table A and its continuation cover the assigned classification, '
    'cite them and complete the classification despite omitted background. A missing footnote that determines '
    'a classification must remain unresolved. Preserve an explicit clause number when only directing readers '
    'to that clause. Read the referenced original when assigned to explain its requirements.'
)


def ordered_components(catalogue, aliases, audit):
    """Use the short writer's dependency and continuation grouping in source order."""
    items = {}
    for key in aliases:
        item = catalogue.aliases[key]
        section = catalogue.sections[item['snapshot']][item['section']]
        items[key] = {'section': (item['snapshot'], item['section']), 'text': catalogue.read(item),
                      'kind': 'asset' if item['locator']['kind'] == 'asset' else 'block',
                      'required_assets': [], 'position': (item['snapshot'],
                          item['locator'].get('span', section['scope'])['start'],
                          item['locator']['kind'] == 'asset', key)}
    items = dict(sorted(items.items(), key=lambda pair: pair[1]['position']))
    relations = [{'item': key, 'role': 'evidence', 'applies_to': []} for key in items]
    by_key = {row['item']: row for row in relations}
    for left, right in audit['dependencies']:
        if (left in by_key and right in by_key and items[left]['section'] == items[right]['section']
                and items[left]['kind'] == items[right]['kind'] == 'block'):
            by_key[left]['applies_to'].append(right)
    _, connected = connected_groups({'items': items}, relations)
    index = {key: position for position, key in enumerate(items)}
    return [sorted(group, key=index.__getitem__) for group in connected]


def slot(service, catalogue, change, read, write, position, parent_headings, input_cap, *, atomic=False,
         writing_revision=1, navigation_headings=(), revision_reason=None):
    refs = [catalogue.aliases[key]['ref'] for key in read]
    assigned_refs = [catalogue.aliases[key]['ref'] for key in write]
    actor, registry = catalogue.access['actor'], catalogue.access['registry_revision']
    reader = service._reader_config()
    assembled = service._assemble_reading(refs, actor, registry, reader)
    measurement = {key: value for key, value in assembled['measurement'].items() if key != 'input_fingerprint'}
    if not measurement['fits']:
        if not atomic or measurement['core_codepoints'] >= input_cap:
            return None, {'kind': 'reader', 'measurement': measurement}
        # One indivisible writing slot can combine separate bounded original
        # reads. The shared wire is still subject to the full request cap.
        sizes = []
        for ref in refs:
            single = service._assemble_reading([ref], actor, registry, reader)['measurement']
            if not single['fits']:
                return None, {'kind': 'reader', 'measurement': measurement,
                              'blocked_unit': ref['unit_ref']['unit_id']}
            sizes.append(single['serialized_codepoints'])
        measurement['individual_read_sizes'] = sizes
    projection = service.preview_reading_model_input(refs, assembled, actor, registry)
    evidence = {key: value for key, value in projection['view'].items() if key != 'input_id'}
    materials = {row['ref']: row for row in evidence['materials']}
    mapping = projection['mapping']
    assigned = [key for key, ref in mapping.items() if ref in assigned_refs]
    require(len(assigned) == len(assigned_refs), 'writing responsibility differs from its authorized evidence')
    task = {'task': 'page', 'materials': evidence['materials']}
    adapter = SimpleNamespace(workflow=SimpleNamespace(knowledge=service, vault=service.vault))
    try:
        images = select_images([task], image_catalog(adapter, {'tasks': [task]}))
    except ContractError as error:
        if error.code == 'SEMANTIC_IMAGE_OVERSIZE':
            return None, {'kind': 'image', 'limit': image_budget(), 'reason': str(error)}
        raise
    page = {key: change[key] for key in ('action', 'page_id', 'change_id', 'name', 'page_type',
                                       'expected_parent_revision_id', 'parent_authored_sha256')}
    view = {'page': page, 'position': position, 'assigned': assigned, 'original_evidence': evidence,
            'existing_headings': parent_headings,
            'instructions': 'Write Markdown beginning at heading level 2 or below. Express every assigned '
                            'requirement within its source scope and cite its supplied handle. Use shared '
                            'material to preserve applicable conditions and exceptions without repeating '
                            'another fragment’s assigned content. For an update, write only the addition '
                            'for this evidence; the complete existing body will be preserved. Preserve explicit '
                            'cross-references to separately assigned clauses as references. Report any missing '
                            'essential original or table explicitly.'}
    if writing_revision != 1:
        view['navigation_headings'] = list(navigation_headings)
        view['instructions'] += ' ' + WRITING_REQUIREMENTS
    if revision_reason is not None:
        view['revision_reason'] = revision_reason
    prepared = {'phase': PHASE, 'view': view, 'schema': SCHEMA, 'material_aliases': mapping,
                'projection': {'view': evidence, 'mapping': mapping},
                'support_refs': assigned_refs, 'reader_measurement': measurement}
    view['preparation_fingerprint'] = preparation_fingerprint(prepared)
    estimate = writing_output({'materials': materials, 'writing_assignment': {'primary': assigned}})
    row = semantic_request(PHASE, view, SCHEMA, estimate, input_cap, images)
    packet = {'prepared': prepared, 'request': row}
    return (packet if row['budget']['fits'] else None), {'kind': 'request', **row['budget']}


def parent_context(service, change):
    if change['action'] == 'create':
        return b'', [], {}
    body = _vault_path(service.vault, change['path']).read_bytes()
    require(_sha(body) == change['parent_authored_sha256'], 'parent body changed before incremental writing')
    headings = re.findall(r'^#{1,6}\s+(.+)$', body.decode('utf-8'), re.M)
    from hermes_source_units.short_build import evidence

    bindings = {}
    root = _vault_path(service.vault, '_system/knowledge-builds')
    for path in sorted(root.glob('short-build-*/manifest.json')):
        run = _load_json(path)
        if not any(row['revision_id'] == change['expected_parent_revision_id'] for row in run['page_revisions']):
            continue
        candidate, = [row for row in evidence(service, run)['scope']['pages']
                      if row['card_path'] == change['path'] and row['authored_sha256'] == change['parent_authored_sha256']]
        bindings = candidate.get('citation_bindings', {})
        break
    retained_handles = set(HANDLE.findall(body.decode('utf-8')))
    require(retained_handles <= bindings.keys(), 'parent inline evidence handles lack their original verified bindings')
    return body, headings, bindings


def prepare_work(service, catalogue, plan, *, input_cap=INPUT_CAP, writing_revision=CURRENT_WRITING_REVISION):
    """Prepare all slots before any execution, so an atomic oversize cannot waste calls."""
    validate_parent(service, plan)
    with service.source.reading_snapshot():
        change, audit = catalogue.writing_support(plan, writing_revision=writing_revision)
        _, headings, _ = parent_context(service, change)
        additions = change['added_support_refs']
        require(additions, 'incremental writing requires substantive new page support')
        aliases = {catalogue.units[canonical_json(ref['unit_ref'])]['alias'] for ref in additions}
        groups = ordered_components(catalogue, aliases, audit)
        ordered = [key for group in groups for key in group]
        links = defaultdict(set)
        for left, right in audit['dependencies']:
            links[left].add(right)

        def reading(assigned):
            relevant = set(assigned)
            pending = list(assigned)
            while pending:
                key = pending.pop()
                dependencies = links[key] - relevant
                relevant.update(dependencies)
                pending.extend(dependencies)
            return [*assigned, *(key for key in catalogue.aliases if key in relevant and key not in assigned)]

        def make(assigned, position, full_input=None, *, atomic=False):
            read = full_input or reading(assigned)
            navigation = catalogue.navigation_titles(read, audit['navigation_aliases']) if writing_revision != 1 else ()
            return slot(service, catalogue, change, read, assigned, position, headings, input_cap, atomic=atomic,
                        writing_revision=writing_revision, navigation_headings=navigation)

        whole, whole_cost = make(ordered, 1)
        fragments = []
        if whole:
            fragments.append(whole)
        else:
            def place(assigned, measured=None, full_input=None):
                position = len(fragments) + 1
                packet, cost = measured or make(assigned, position, full_input)
                if not packet and cost['kind'] == 'reader':
                    packet, cost = make(assigned, position, full_input, atomic=True)
                if packet:
                    fragments.append(packet)
                    return
                input_fits = (cost['kind'] == 'request'
                              and cost['input_size'] + cost['input_reserve'] <= cost['input_cap']
                              and cost['image_count'] <= image_budget()['max_images']
                              and cost['image_bytes'] <= image_budget()['max_bytes'])
                if input_fits and cost['estimated_output_tokens'] > cost['usable_output_tokens'] and len(assigned) > 1:
                    read = full_input or reading(assigned)
                    half = len(assigned) // 2
                    for part in (assigned[:half], assigned[half:]):
                        place(part, full_input=read)
                    return
                labels = '; '.join(f'{key} {catalogue.label(key)[1]}' for key in assigned)
                raise ContractError('PAGE_FRAGMENT_OVERSIZE', '$', f'complete clause evidence exceeds the {cost["kind"]} budget: {labels}')

            # First retain a complete reading across an output-only split.
            if (whole_cost['kind'] == 'request' and whole_cost['input_size'] <= input_cap
                    and whole_cost['estimated_output_tokens'] > whole_cost['usable_output_tokens']
                    and whole_cost['image_count'] <= image_budget()['max_images']
                    and whole_cost['image_bytes'] <= image_budget()['max_bytes']):
                place(ordered, (whole, whole_cost))
            else:
                def section(group):
                    item = catalogue.aliases[group[0]]
                    return item['snapshot'], item['section']

                for _, components in groupby(groups, key=section):
                    components = list(components)
                    complete = [key for group in components for key in group]
                    measured = make(complete, len(fragments) + 1)
                    if measured[0] or len(components) == 1:
                        place(complete, measured)
                        continue
                    pending = []
                    for group in components:
                        trial = [*pending, *group]
                        if pending and not make(trial, len(fragments) + 1)[0]:
                            place(pending)
                            pending = []
                        pending.extend(group)
                    if pending:
                        place(pending)
    work = {'contract': CONTRACT, 'plan_id': plan['change_id'], 'change': change, 'selection': audit,
            'whole_cost': whole_cost, 'fragments': fragments, 'input_cap': input_cap}
    if writing_revision != 1:
        work['writing_revision'] = writing_revision
    return work


def assemble(service, work, answers):
    """Require all native parts before assembling one version, retaining exact parent bytes."""
    change = work['change']
    for position, fragment in enumerate(work['fragments'], 1):
        view = fragment['prepared']['view']
        require(view['position'] == position and view['page']['change_id'] == change['change_id'],
                'fragment position or page revision binding changed')
    return compose(service, change, zip(work['fragments'], answers, strict=True))


def compose(service, change, fragment_answers):
    """Assemble already bound native fragments and retain the exact parent bytes."""
    validate_parent(service, change)
    parent, _, bindings = parent_context(service, change)
    bindings = dict(bindings)
    reverse = {canonical_json(ref): key for key, ref in bindings.items() if HANDLE.fullmatch(f'[{key}]')}
    taken = set(HANDLE.findall(parent.decode('utf-8'))) | set(bindings)
    index = 1
    bodies, expressed = [], []
    for fragment, answer in fragment_answers:
        prepared = fragment['prepared']
        content = validate_page_revision(prepared, answer)
        body = content['body'].decode('utf-8')
        require(not re.search(r'^#\s', body, re.M) and not body.lstrip().startswith('---'),
                'fragment cannot replace the page title or metadata')
        expressed.extend(prepared['support_refs'])
        replacements = {}
        for key, ref in content['citation_bindings'].items():
            identity = canonical_json(ref)
            if identity not in reverse:
                while f'm{index}' in taken:
                    index += 1
                alias = f'm{index}'
                taken.add(alias)
                bindings[alias] = ref
                reverse[identity] = alias
            replacements[key] = reverse[identity]
        bodies.append(HANDLE.sub(lambda match: f'[{replacements[match[1]]}]', body).rstrip())
    require(len(expressed) == len(unique_refs(expressed)) and
            {canonical_json(ref) for ref in expressed} == {canonical_json(ref) for ref in change['added_support_refs']},
            'fragment writing responsibilities must cover each new support exactly once')
    prefix = parent if parent else f"# {change['name']}\n".encode()
    separator = b'\n' if prefix.endswith(b'\n') else b'\n\n'
    body = prefix + separator + ('\n\n'.join(bodies) + '\n').encode('utf-8')
    return {'body': body, 'authored_sha256': _sha(body), 'citation_bindings': bindings,
            'support_refs': unique_refs([*change['retained_support_refs'], *change['added_support_refs']])}


def catalogue_for_round(domain, run_id, frozen, service, *, unavailable=None):
    from hermes_source_units.incremental_sources import read_pin

    hydrated = []
    for mid in (*frozen['map_result_ids'], *frozen.get('context_map_result_ids', [])):
        try:
            mapped = domain.read_map(run_id, mid)
            hydrated.append({**mapped, 'mapped': read_pin(domain.vault, mapped['results'][0])})
        except ContractError as error:
            if unavailable is None or error.code == 'REVISION_CONFLICT':
                raise
            unavailable[mid] = str(error)
    return EvidenceCatalogue(service, hydrated, domain.status(run_id)['actor'])


def selected_change(vault, pointer):
    from hermes_source_units.incremental_sources import read_pin

    saved = read_pin(vault, pointer['preparation'])
    return saved['change'] if 'change' in saved else saved['prepared']['view']['change']


def fragment_answers(vault, root, work):
    operation = _load_json(root / 'operation.json')
    if operation.get('writing_continuation'):
        from incremental_writing_resume import continued_answers

        return continued_answers(vault, root, work, operation['writing_continuation'])
    answers, proofs = [], []
    for fragment in work['fragments']:
        answer, proof = fragment_receipt(vault, root, fragment)
        answers.append(answer)
        proofs.append(proof)
    return answers, proofs


def fragment_receipt(vault, root, fragment):
    from hermes_source_units.incremental_sources import pinned
    from incremental_native import verified_result

    row = fragment['request']
    result, proof = verified_result(root, row)
    require(result['state'] == 'settled', 'page has an unsettled native writing fragment')
    receipt = root / 'receipts' / (row['id'] + '.json')
    return _load_json(receipt)['answer'], {
        **proof, 'request_id': row['id'], 'receipt': pinned(vault, receipt.relative_to(vault).as_posix())}


def qualify_held_sections(domain, service, frozen, round_root, writer, changes, work):
    from hermes_source_units.incremental_sources import pinned
    from incremental_sections import (ORDINARY_QUALIFICATION, current_ordinary_sections, ordinary_candidates,
                                      ordinary_record, section_placements)
    from prepare_short_evidence_test import save
    from short_cards import snapshot as card_layout

    if not work:
        return [], [], {}
    vault = domain.vault
    path = writer / 'section-qualification.json'
    batch = {'contract': ORDINARY_QUALIFICATION, 'run_id': frozen['run_id'], 'round_id': frozen['round_id'],
             'round_input_fingerprint': frozen['input_fingerprint'],
             'plans': pinned(vault, (round_root / 'page-changes.json').relative_to(vault).as_posix()),
             'parents': [{'plan_id': plan['change_id'], 'work': pinned(vault, prep.relative_to(vault).as_posix())}
                         for plan, prep, _ in work]}
    if path.is_file():
        existing = _load_json(path)
        require({key: existing[key] for key in batch if key != 'parents'}
                == {key: val for key, val in batch.items() if key != 'parents'},
                'ordinary independent section inputs changed before disposition')
        original_parents = {row['plan_id']: row['work'] for row in existing['parents']}
        require(all(original_parents[row['plan_id']] == row['work'] for row in batch['parents']
                    if row['plan_id'] in original_parents), 'ordinary independent section parent work changed')
        work = [entry for entry in work if entry[0]['change_id'] in original_parents]
        layout, placements = existing['layout'], existing['placements']
    else:
        layout = card_layout(vault)
        layout['occupied'] = sorted(set(layout['occupied']) | {plan['path'] for plan in changes if plan.get('path')})
        placements = None
    parents = [(plan, saved) for plan, _, saved in work]
    try:
        cards, deferred = current_ordinary_sections(domain, service, frozen, lambda catalogue, unavailable:
            ordinary_candidates(vault, service, catalogue, writer, layout, parents,
                                placements=placements, unavailable=unavailable))
    except (ContractError, ValueError, OSError) as error:
        return [], [], {plan['change_id']: [{'reason': str(error)}] for plan, _ in parents}
    if not cards:
        return [], [], deferred
    if placements is None:
        save(path, {**batch, 'layout': layout, 'placements': section_placements(cards)})
    qualification = pinned(vault, path.relative_to(vault).as_posix())
    written, accepted = [], []
    for change, entries in cards:
        path = writer / 'independent-sections' / (change['change_id'] + '.json')
        save(path, ordinary_record(change, entries, qualification))
        written.append({'preparation': pinned(vault, path.relative_to(vault).as_posix()),
                        'native_root': writer.relative_to(vault).as_posix()})
        accepted.append(change)
    return written, accepted, deferred


def writing_disposition(domain, control, scope, frozen, changes, service, maps):
    from hermes_source_units.incremental_sources import pinned
    from incremental_runtime import execute_rows, initialize_scope
    from prepare_short_evidence_test import save

    round_id, run_id = scope['scope_id'], frozen['run_id']
    root = domain.root(run_id) / 'rounds' / round_id
    original_path = root / 'writing-disposition.json'
    head = control.status()
    amendment = head.get('writing_amendments', {}).get(round_id)
    if amendment:
        from incremental_writing_amendment import disposition_path, execute

        output = disposition_path(domain, run_id, round_id, amendment['operation_id'])
        if output.is_file():
            return output, _load_json(output)
        with control.executing(scope, 'writing', frozen['input_fingerprint']):
            return output, execute(domain, service, frozen, amendment, output)
    repair = head.get('writing_repairs', {}).get(round_id)
    path = root / 'writing-revision.json' if repair else original_path
    if path.is_file():
        return path, _load_json(path)
    continuation = control.status().get('writing_continuations', {}).get(round_id)
    if continuation:
        from incremental_writing_resume import execute_continuation

        with control.executing(scope, 'writing', frozen['input_fingerprint']):
            return path, execute_continuation(domain, frozen, changes, service, maps, continuation, path)
    written, held = [], []
    if repair:
        require(_sha(original_path.read_bytes()) == repair['prior_disposition_sha256'],
                'original writing checkpoint changed before repair')
        prior = _load_json(original_path)
        selected = set(repair['plan_ids'])
        changes = [change for change in changes if change['change_id'] in selected]
        written = list(prior['written'])
        held = [row for row in prior['held'] if row['change_id'] not in selected]
    writer, operation = initialize_scope(domain, run_id, 'round', round_id, 'writing', frozen['snapshots'],
                                         recovery_id=repair['operation_id'] if repair else None)
    with control.executing(scope, 'writing', frozen['input_fingerprint']):
        catalogue = EvidenceCatalogue(service, maps, operation['actor'])
        prior_fragments = sorted((writer / 'fragment-plans').glob('*.json'))
        # A resumed writer retains the same input contract as its saved work.
        writing_revision = (_load_json(prior_fragments[0]).get('writing_revision', 1)
                            if prior_fragments else CURRENT_WRITING_REVISION)
        work = []
        rows = []
        for plan in changes:
            if plan['action'] == 'hold':
                held.append(plan)
                continue
            if plan['action'] == 'reuse':
                continue
            plan_id = plan['change_id']
            try:
                legacy = writer / 'preparations' / (plan_id + '.json')
                if legacy.is_file():
                    saved = _load_json(legacy)
                    from hermes_source_units.incremental_pages import validate_preparation

                    require(saved['prepared']['view']['change'] == plan,
                            'saved legacy writing preparation differs from its original plan')
                    validate_preparation(service, saved['prepared'], operation['actor'])
                    prep, request_rows = legacy, [saved['request']]
                else:
                    prep = writer / 'fragment-plans' / (plan_id + '.json')
                    saved = prepare_work(service, catalogue, plan, writing_revision=writing_revision)
                    save(prep, saved)
                    request_rows = [fragment['request'] for fragment in saved['fragments']]
                work.append((plan, prep, saved))
                rows.extend(request_rows)
            except (ContractError, ValueError, OSError) as error:
                held.append({'change_id': plan_id, 'page_id': plan['page_id'], 'state': 'held', 'reason': str(error)})
        if rows:
            # Legacy in-flight preparations retain their original request and budget.
            execute_rows(domain, run_id, writer, operation, rows)
        restored = set()
        held_work = []
        for plan, prep, saved in work:
            try:
                if saved.get('contract') == CONTRACT:
                    answers, _ = fragment_answers(domain.vault, writer, saved)
                    assemble(service, saved, answers)
                else:
                    answer = _load_json(writer / 'receipts' / (saved['request']['id'] + '.json'))['answer']
                    validate_page_revision(saved['prepared'], answer)
                written.append({'preparation': pinned(domain.vault, prep.relative_to(domain.vault).as_posix()),
                                'native_root': writer.relative_to(domain.vault).as_posix()})
                restored.add(plan['change_id'])
            except (ContractError, ValueError, OSError) as error:
                held.append({'change_id': plan['change_id'], 'page_id': plan['page_id'],
                             'state': 'held', 'reason': str(error)})
                if not repair and saved.get('contract') == CONTRACT:
                    held_work.append((plan, prep, saved))
        disposition = {'written': written, 'held': held}
        if repair:
            disposition['repair'] = repair
            if 'independent_sections' in prior:
                kept, cards = [], []
                for row in written:
                    change = selected_change(domain.vault, row)
                    section = change.get('independent_section')
                    if section and section['parent_plan_id'] in restored:
                        continue
                    if section:
                        cards.append(change)
                    kept.append(row)
                disposition.update(written=kept, original_pages=len(kept) - len(cards), independent_sections=cards,
                                   section_holds={key: rows for key, rows in prior.get('section_holds', {}).items()
                                                  if key not in restored})
        else:
            sections, cards, deferred = qualify_held_sections(domain, service, frozen, root, writer, changes, held_work)
            disposition.update(written=[*written, *sections], original_pages=len(written),
                               independent_sections=cards, section_holds=deferred)
        save(path, disposition)
    return path, disposition
