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
from incremental_writing_evidence import EvidenceCatalogue
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
        if left in by_key and right in by_key:
            by_key[left]['applies_to'].append(right)
    _, connected = connected_groups({'items': items}, relations)
    index = {key: position for position, key in enumerate(items)}
    return [sorted(group, key=index.__getitem__) for group in connected]


def slot(service, catalogue, change, read, write, position, parent_headings, input_cap):
    refs = [catalogue.aliases[key]['ref'] for key in read]
    assigned_refs = [catalogue.aliases[key]['ref'] for key in write]
    actor, registry = catalogue.access['actor'], catalogue.access['registry_revision']
    assembled = service._assemble_reading(refs, actor, registry, service._reader_config())
    measurement = {key: value for key, value in assembled['measurement'].items() if key != 'input_fingerprint'}
    if not measurement['fits']:
        return None, {'kind': 'reader', 'measurement': measurement}
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
                            'for this evidence; the complete existing body will be preserved. Report any '
                            'missing essential original or table explicitly.'}
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
        bindings = candidate['citation_bindings']
        break
    retained_handles = set(HANDLE.findall(body.decode('utf-8')))
    require(retained_handles <= bindings.keys(), 'parent inline evidence handles lack their original verified bindings')
    return body, headings, bindings


def prepare_work(service, catalogue, plan, *, input_cap=INPUT_CAP):
    """Prepare all slots before any execution, so an atomic oversize cannot waste calls."""
    validate_parent(service, plan)
    with service.source.reading_snapshot():
        change, audit = catalogue.writing_support(plan)
        _, headings, _ = parent_context(service, change)
        additions = change['added_support_refs']
        require(additions, 'incremental writing requires substantive new page support')
        aliases = {catalogue.units[canonical_json(ref['unit_ref'])]['alias'] for ref in additions}
        groups = ordered_components(catalogue, aliases, audit)
        ordered = [key for group in groups for key in group]
        # Retained evidence may support a new requirement without being rewritten.
        shared = set(audit['qualified_aliases']) - aliases
        links = defaultdict(set)
        for left, right in audit['dependencies']:
            links[left].add(right)
            links[right].add(left)

        def reading(assigned):
            relevant = set(assigned)
            pending = list(assigned)
            while pending:
                key = pending.pop()
                dependencies = (links[key] & shared) - relevant
                relevant.update(dependencies)
                pending.extend(dependencies)
            return [key for key in catalogue.aliases if key in relevant and key in shared] + assigned

        def make(assigned, position, full_input=None):
            return slot(service, catalogue, change, full_input or reading(assigned), assigned,
                        position, headings, input_cap)

        whole, whole_cost = make(ordered, 1)
        fragments = []
        if whole:
            fragments.append(whole)
        else:
            def place(assigned, measured=None, full_input=None):
                packet, cost = measured or make(assigned, len(fragments) + 1, full_input)
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
    return {'contract': CONTRACT, 'plan_id': plan['change_id'], 'change': change, 'selection': audit,
            'whole_cost': whole_cost, 'fragments': fragments, 'input_cap': input_cap}


def assemble(service, work, answers):
    """Require all native parts before assembling one version, retaining exact parent bytes."""
    change = work['change']
    validate_parent(service, change)
    parent, _, bindings = parent_context(service, change)
    bindings = dict(bindings)
    reverse = {canonical_json(ref): key for key, ref in bindings.items() if HANDLE.fullmatch(f'[{key}]')}
    taken = set(HANDLE.findall(parent.decode('utf-8'))) | set(bindings)
    index = 1
    bodies, expressed = [], []
    for position, (fragment, answer) in enumerate(zip(work['fragments'], answers, strict=True), 1):
        prepared = fragment['prepared']
        require(prepared['view']['position'] == position and prepared['view']['page']['change_id'] == change['change_id'],
                'fragment position or page revision binding changed')
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


def catalogue_for_round(domain, run_id, frozen, service):
    from hermes_source_units.incremental_sources import read_pin

    maps = [domain.read_map(run_id, mid) for mid in [*frozen['map_result_ids'], *frozen.get('context_map_result_ids', [])]]
    hydrated = [{**mapped, 'mapped': read_pin(domain.vault, mapped['results'][0])} for mapped in maps]
    return EvidenceCatalogue(service, hydrated, domain.status(run_id)['actor'])


def selected_change(vault, pointer):
    from hermes_source_units.incremental_sources import read_pin

    saved = read_pin(vault, pointer['preparation'])
    return saved['change'] if saved.get('contract') == CONTRACT else saved['prepared']['view']['change']


def fragment_answers(vault, root, work):
    from hermes_source_units.incremental_sources import pinned
    from incremental_native import verified_result

    answers, proofs = [], []
    for fragment in work['fragments']:
        row = fragment['request']
        result, proof = verified_result(root, row)
        require(result['state'] == 'settled', 'page has an unsettled native writing fragment')
        receipt = root / 'receipts' / (row['id'] + '.json')
        proofs.append({**proof, 'request_id': row['id'], 'receipt': pinned(vault, receipt.relative_to(vault).as_posix())})
        answers.append(_load_json(receipt)['answer'])
    return answers, proofs


def writing_disposition(domain, control, scope, frozen, changes, service, maps):
    from hermes_source_units.incremental_sources import pinned
    from incremental_runtime import execute_rows, initialize_scope
    from prepare_short_evidence_test import save

    round_id, run_id = scope['scope_id'], frozen['run_id']
    root = domain.root(run_id) / 'rounds' / round_id
    original_path = root / 'writing-disposition.json'
    repair = control.status().get('writing_repairs', {}).get(round_id)
    path = root / 'writing-revision.json' if repair else original_path
    if path.is_file():
        return path, _load_json(path)
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
                    saved = prepare_work(service, catalogue, plan)
                    save(prep, saved)
                    request_rows = [fragment['request'] for fragment in saved['fragments']]
                work.append((plan, prep, saved))
                rows.extend(request_rows)
            except (ContractError, ValueError, OSError) as error:
                held.append({'change_id': plan_id, 'page_id': plan['page_id'], 'state': 'held', 'reason': str(error)})
        if rows:
            # Legacy in-flight preparations retain their original request and budget.
            execute_rows(domain, run_id, writer, operation, rows)
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
            except (ContractError, ValueError, OSError) as error:
                held.append({'change_id': plan['change_id'], 'page_id': plan['page_id'],
                             'state': 'held', 'reason': str(error)})
        disposition = {'written': written, 'held': held}
        if repair:
            disposition['repair'] = repair
        save(path, disposition)
    return path, disposition
