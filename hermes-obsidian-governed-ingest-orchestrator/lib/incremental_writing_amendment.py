"""Review held incremental fragments at their exact writing checkpoint."""
from collections import Counter, defaultdict
import re

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_pages import require, unique_refs, validate_page_revision
from hermes_source_units.incremental_sources import read_pin, safe_id
from hermes_source_units.knowledge_build import FileKnowledgeBuildService
from hermes_source_units.source_units import _exclusive_lock, _load_json, _vault_path
from hermes_source_units.validation import ContractError, canonical_json, fingerprint
from incremental_recovery import stopped_history
from incremental_sections import aliases, candidates, proposals
from incremental_writing import (
    CURRENT_WRITING_REVISION, catalogue_for_round, compose, fragment_receipt, ordered_components,
    parent_context, prepare_work, selected_change, slot,
)
from incremental_writing_resume import IDENTITY, budget_allowance, continued_fragment, owned, pin
from prepare_short_evidence_test import save
from short_cards import snapshot as card_layout

CONTRACT = 'incremental-writing-amendment/v1'
PORTABLE_CITATION = re.compile(r'(?<=[^\s`(（\[])〔(m[0-9]+)〕|(?<=[^\s`(（\[])（来源[：:](m[0-9]+)）')
UNREADABLE = re.compile(r'不可辨认|无法辨认|乱码|unreadable|corrupt|\bOCR\b', re.I)


def validate_fragment(fragment, answer):
    content = validate_page_revision(fragment['prepared'], answer)
    body = content['body'].decode('utf-8')
    require(not re.search(r'^#\s', body, re.M) and not body.lstrip().startswith('---'),
            'fragment cannot replace the page title or metadata')


def mechanical_review(fragment, answer):
    """Normalize an unambiguous adjacent marker without adding any inspection or content."""
    if answer['unresolved']:
        return None
    output = []
    for line in answer['content'].splitlines(keepends=True):
        # Handle a reference to one material at the end of an ordinary prose
        # line. Leave grouped marks, Markdown structures and code to the worker.
        if re.match(r'^\s*[\x60~#>|]', line) or '`' in line:
            output.append(line)
            continue
        output.append(PORTABLE_CITATION.sub(lambda match: f'[{match[1] or match[2]}]', line))
    revised = {**answer, 'content': ''.join(output)}
    if revised == answer:
        return None
    try:
        validate_fragment(fragment, revised)
    except ContractError:
        return None
    return revised


def classify(fragment, answer):
    try:
        validate_fragment(fragment, answer)
    except ContractError as error:
        corrected = mechanical_review(fragment, answer)
        if corrected is not None:
            return {'state': 'valid', 'answer': corrected,
                    'review': {'rule': 'adjacent-single-material-citation/v1', 'answer_sha256': fingerprint(corrected)}}
        return {'state': 'held', 'answer': answer, 'reason': str(error)}
    return {'state': 'valid', 'answer': answer}


def observed(vault, writer, work, continuation, history):
    slots = {}
    page = continuation['pages'][work['plan_id']] if continuation else None
    originals = ({item['request']['id']: item for item in read_pin(vault, page['original_work'])['fragments']}
                 if page else {})
    for index, fragment in enumerate(work['fragments']):
        identity = fragment['request']['id']
        row = {'fragment': fragment, 'state': 'unqualified'}
        if page and page['slots'][index]['disposition'] == 'reuse':
            answer, proof = continued_fragment(vault, writer, fragment, page['slots'][index], continuation, page)
            row.update(classify(fragment, answer), proof=proof)
        elif identity in history:
            entry = history[identity]
            if entry.get('native_state') == 'settled':
                answer, proof = fragment_receipt(vault, writer, fragment)
                row.update(classify(fragment, answer), proof=proof, attempts=entry['attempts'])
            elif entry['attempts']:
                row['state'] = 'unknown'
        elif page:
            decision = page['slots'][index]
            prior = decision['prior_requests']
            if any(continuation['history'][key]['state'] == 'isolated_attempt' for key in prior):
                row['state'] = 'unknown'
            elif len(prior) == 1:
                previous = originals[prior[0]]
                entry = continuation['history'][prior[0]]
                if (entry['state'] == 'held' and entry.get('receipt') and owned(previous) == owned(fragment)
                        and previous['prepared']['projection'] == fragment['prepared']['projection']):
                    original_writer = _vault_path(vault, continuation['previous_operation']['ref']).parent
                    answer, proof = fragment_receipt(vault, original_writer, previous)
                    require(proof['receipt'] == entry['receipt'], 'original held receipt moved before review')
                    row.update(classify(fragment, answer), proof=proof, attempts=entry['attempts'])
        slots[identity] = row
    return slots


def revise_requests(builder, catalogue, work, slots, identity):
    prior = slots[identity]
    if prior['state'] != 'held' or len(prior.get('attempts', [])) != 1:
        return None, 'requires a confirmed held reply with one prior reservation'
    fragment = prior['fragment']
    prepared = fragment['prepared']
    selected = aliases(catalogue, prepared['support_refs'])
    reading = aliases(catalogue, prepared['material_aliases'].values())
    try:
        original = catalogue.referenced_original_clauses(reading, ' '.join(prior['answer']['unresolved']))
        continuation = catalogue.clause_continuations(selected | original)
        required = original | {right for _, right in continuation}
        assets = catalogue.explicit_dependencies(selected | required)
        required.update(right for _, right in assets)
    except ContractError as error:
        return None, str(error)
    extra = required - reading
    if UNREADABLE.search(' '.join(prior['answer']['unresolved'])) or (prior['answer']['unresolved'] and not extra):
        return None, 'material gap requires a verified corrected input'
    unknown = set().union(*(aliases(catalogue, row['fragment']['prepared']['support_refs'])
                            for row in slots.values() if row['state'] == 'unknown'))
    if unknown & (reading | extra):
        return None, 'required material overlaps an unconfirmed fragment'
    other_owners = set().union(*(aliases(catalogue, row['fragment']['prepared']['support_refs'])
                                 for key, row in slots.items() if key != identity))
    updated_owners = selected | (extra - other_owners)
    cause = 'required_source_supplement' if extra else 'correct_coverage_citation_or_inspection'
    binding = {'classification': cause, 'original_request_id': prior['proof']['request_id'],
               'original_receipt_sha256': prior['proof']['receipt']['sha256'],
               'original_evidence_sha256': fingerprint(prepared['projection']),
               'expected_owner': sorted(selected), 'new_required_aliases': sorted(extra),
               'reason': prior['reason'], 'unresolved': prior['answer']['unresolved'],
               'prior_reservations': [row['budget_id'] for row in prior['attempts']],
               'original_deficiencies': {
                   'uncited': sorted(set(prepared['view']['assigned']) - set(re.findall(r'\[(m[0-9]+)\]', prior['answer']['content']))),
                   'uninspected': sorted(set(prepared['material_aliases']) - set(prior['answer']['inspected'])),
               }}
    order = list(dict.fromkeys([*(catalogue.units[canonical_json(ref['unit_ref'])]['alias']
                                for ref in prepared['material_aliases'].values()), *sorted(extra)]))
    _, headings, _ = parent_context(builder, work['change'])
    def packet(assigned, read, position=None):
        detail = binding if position is None else {**binding, 'split_position': position}
        return slot(builder, catalogue, work['change'], read, assigned, prepared['view']['position'], headings,
                    work['input_cap'], atomic=True, writing_revision=CURRENT_WRITING_REVISION,
                    navigation_headings=prepared['view'].get('navigation_headings', []), revision_reason=detail)

    with builder.source.reading_snapshot():
        candidate, measurement = packet([key for key in order if key in updated_owners], order)
        if candidate:
            return {'fragments': [candidate]}, cause
        # Keep the authenticated shared reading on each part. Split ownership
        # only where established dependencies permit independent work.
        edges = [*map(tuple, work['selection']['dependencies']),
                 *map(tuple, work['selection']['clause_cross_references']), *continuation, *assets]
        edges.extend((left, right) for left in selected for right in original)
        neighbors = defaultdict(set)
        for left, right in edges:
            neighbors[left].add(right)
            neighbors[right].add(left)
        groups = ordered_components(catalogue, updated_owners, {'dependencies': edges})
        if len(groups) > 1:
            shared = set(order) - updated_owners
            parts = []
            for n, assigned in enumerate(groups, 1):
                needed = set(assigned) | shared
                todo = list(needed)
                while todo:
                    dependencies = (neighbors[todo.pop()] & set(order)) - needed
                    needed.update(dependencies)
                    todo.extend(dependencies)
                candidate, measurement = packet(assigned, [key for key in order if key in needed], n)
                if not candidate:
                    break
                parts.append(candidate)
            if len(parts) == len(groups):
                return {'fragments': parts}, f'{cause}: split into {len(parts)} bounded assignments'
    return None, f'complete affected evidence exceeds {measurement["kind"]} budget'


def base_paths(domain, run_id, round_id, head):
    root = domain.root(run_id)
    parent = root / 'rounds' / round_id
    repair = head.get('writing_repairs', {}).get(round_id)
    continuation = head.get('writing_continuations', {}).get(round_id)
    base = parent / ('writing-revision.json' if repair else 'writing-disposition.json')
    writer = root / 'native' / 'round' / round_id / 'writing'
    if repair:
        writer = writer / 'recoveries' / safe_id((continuation or repair)['operation_id'])
    return base, writer


def inspect_base(domain, builder, frozen, base, catalogue):
    vault = domain.vault
    disposition = read_pin(vault, base['disposition'])
    continuation = read_pin(vault, disposition['writing_continuation']) if 'writing_continuation' in disposition else None
    operation = read_pin(vault, base['operation'])
    writer = _vault_path(vault, base['operation']['ref']).parent
    work = {key: read_pin(vault, pointer) for key, pointer in base['work'].items()}
    require(all(key == value['plan_id'] for key, value in work.items()), 'writing amendment base work changed')
    plans = {plan['change_id']: plan for plan in read_pin(vault, base['changes'])}
    all_requests = []
    for key, item in work.items():
        require(prepare_work(builder, catalogue, plans[key], writing_revision=item.get('writing_revision', 1)) == item,
                'saved base fragment no longer matches the frozen plan')
        if continuation:
            decisions = continuation['pages'][key]['slots']
            all_requests.extend(part['request'] for part, decision in zip(item['fragments'], decisions, strict=True)
                                if decision['disposition'] == 'dispatch')
        else:
            all_requests.extend(part['request'] for part in item['fragments'])
    if continuation:
        require(set(continuation['dispatch_request_ids']) == {row['id'] for row in all_requests},
                'writing amendment base lost a previously dispatched request')
    run = domain.status(frozen['run_id'])
    history = stopped_history(vault, writer, operation, all_requests, run['reservations'])
    with builder.source.reading_snapshot():
        slots = {key: observed(vault, writer, item, continuation, history) for key, item in work.items()}
    return plans, disposition, work, slots


def evaluation(domain, builder, frozen, manifest, catalogue):
    vault = domain.vault
    plans, base, work, slots = inspect_base(domain, builder, frozen, manifest['base'], catalogue)
    prior_held = {item['change_id']: item for item in base['held']}
    prior_written = [(row, selected_change(vault, row)) for row in base['written']]
    prior_sections = {change['identity']['identity_key'] for _, change in prior_written if change.get('independent_section')}
    restored = set()
    held, pages, sections, section_holds, summaries = [], [], [], {}, {}
    for plan_id, original in prior_held.items():
        if plan_id not in work:
            held.append(original)
            section_holds[plan_id] = [{'reason': 'no complete historical fragment plan to verify'}]
            continue
        original_scope = slots[plan_id]
        scope = {}
        for identity, row in original_scope.items():
            if identity not in manifest['revisions']:
                scope[identity] = row
                continue
            revision = manifest['revisions'][identity]
            candidate, _ = revise_requests(builder, catalogue, work[plan_id], original_scope, identity)
            require(candidate is not None and candidate == read_pin(vault, revision['preparation']),
                    'targeted writing request differs from its original receipt and live source')
            for part in candidate['fragments']:
                key = part['request']['id']
                try:
                    answer, proof = fragment_receipt(vault, _vault_path(vault, revision['native_root']), part)
                except (ContractError, FileNotFoundError) as error:
                    scope[key] = {'fragment': part, 'state': 'held', 'reason': str(error)}
                    continue
                scope[key] = {**classify(part, answer), 'fragment': part, 'proof': proof}
        slots[plan_id] = scope
        summaries[plan_id] = dict(Counter(row['state'] for row in scope.values()))
        if all(row['state'] == 'valid' for row in scope.values()):
            added = unique_refs(ref for row in scope.values() for ref in row['fragment']['prepared']['support_refs'])
            change = {**work[plan_id]['change'], 'added_support_refs': added}
            change.pop('change_id')
            change['change_id'] = fingerprint(change)
            entries = list(scope.values())
            compose(builder, change, ((x['fragment'], x['answer']) for x in entries))
            pages.append((change, entries))
            restored.add(plan_id)
            continue
        blockers = [row for row in scope.values() if row['state'] != 'valid']
        held.append({**original, 'reason': next((row['reason'] for row in blockers if 'reason' in row), original['reason'])})
        with builder.source.reading_snapshot():
            result, section_holds[plan_id] = proposals(catalogue, plans[plan_id], work[plan_id], scope)
        sections.extend(item for item in result if f"short-evidence:{item['id']}" not in prior_sections)
    cards = candidates(builder, manifest['layout'], plans, sections, slots)
    for change, entries in cards:
        compose(builder, change, ((x['fragment'], x['answer']) for x in entries))
    existing = [row for row, change in prior_written
                if change.get('independent_section', {}).get('parent_plan_id') not in restored]
    return existing, held, pages, cards, section_holds, summaries


def carried_sections(vault, written):
    return [change for row in written if (change := selected_change(vault, row)).get('independent_section')]


def disposition_path(domain, run_id, round_id, operation_id):
    return domain.root(run_id) / 'rounds' / round_id / 'writing-amendments' / operation_id / 'disposition.json'


def make_plan(domain, request, head):
    run_id, round_id = request['run_id'], request['round_id']
    run = domain.status(run_id)
    boundary = head['boundary'] or {}
    require(head['state'] == 'paused' and boundary.get('stage') == 'writing' and not head['active_scopes']
            and request['actor'] == run['actor'] == head['actor'] and run['state'] == 'active'
            and round_id == run['active_round_id'] == boundary['scope']['scope_id']
            and request['expected_revision'] == head['revision'] and request['run_revision'] == run['revision']
            and request['boundary_fingerprint'] == fingerprint(boundary) and 'writing' in head['pause_after'],
            'targeted revision needs the exact drained writing checkpoint')
    vault = domain.vault
    base_path, writer = base_paths(domain, run_id, round_id, head)
    prior = head.get('writing_amendments', {}).get(round_id)
    prior_path = disposition_path(domain, run_id, round_id, prior['operation_id']) if prior else base_path
    previous = read_pin(vault, prior['manifest']) if prior else None
    prior_pointer = pin(vault, prior_path)
    require(prior_pointer['sha256'] == boundary['evidence']['writing_disposition_sha256']
            and not (base_path.parent / 'formal-request.json').exists(), 'writing checkpoint changed before review')
    base = previous['base'] if previous else {
        'disposition': pin(vault, base_path), 'operation': pin(vault, writer / 'operation.json'),
        'changes': pin(vault, base_path.parent / 'page-changes.json'),
        'work': {path.stem: pin(vault, path) for path in sorted((writer / 'fragment-plans').glob('*.json'))}}
    builder = FileKnowledgeBuildService(vault)
    frozen = domain.round_input(run_id, round_id)
    catalogue = catalogue_for_round(domain, run_id, frozen, builder)
    _, _, work, slots = inspect_base(domain, builder, frozen, base, catalogue)
    request_ids = {key: plan_id for plan_id, rows in slots.items() for key in rows}
    prior_outcome = read_pin(vault, prior_pointer)
    previously = prior_outcome['revisions'] if previous else {}
    allowed = set(request['revise_fragments'])
    require(allowed <= request_ids.keys(), 'requested fragment is outside the prior writing plan')
    revised = {}
    decisions = {}
    held = {item['change_id'] for item in prior_outcome['held']}
    new_owners = {}
    for key in request['revise_fragments']:
        plan_id = request_ids[key]
        if key in previously or plan_id not in held:
            decisions[key] = 'already revised or parent already complete'
            continue
        with builder.source.reading_snapshot():
            candidate, reason = revise_requests(builder, catalogue, work[plan_id], slots[plan_id], key)
        decisions[key] = reason
        if candidate:
            extra_owner = set().union(*(owned(part) for part in candidate['fragments'])) - owned(slots[plan_id][key]['fragment'])
            occupied = new_owners.setdefault(plan_id, set())
            if extra_owner & occupied:
                decisions[key] = 'another targeted slot owns the new evidence'
                continue
            occupied.update(extra_owner)
            revised[key] = candidate
    rows = [part['request'] for value in revised.values() for part in value['fragments']]
    budget_allowance(run, rows)
    base_cards = carried_sections(vault, read_pin(vault, base['disposition'])['written'])
    return {'contract': CONTRACT, **{field: request[field] for field in IDENTITY - {'expected_revision'}},
            'control_revision': head['revision'], 'revise_fragments': request['revise_fragments'],
            'frozen_input': frozen['input_fingerprint'], 'base': base, 'prior_disposition': prior_pointer,
            'previous_amendment': prior['manifest'] if prior else None, 'layout': previous['layout'] if previous else
            {**(layout := card_layout(vault)), 'occupied': sorted(set(layout['occupied']) | {
                plan['path'] for plan in [*read_pin(vault, base['changes']), *base_cards] if plan.get('path')})},
            'revisions': previously, 'proposed': revised, 'decisions': decisions,
            'dispatch_request_ids': [row['id'] for row in rows]}


def complete_manifest(domain, manifest, writer):
    vault = domain.vault
    prepared = dict(manifest['revisions'])
    for key, fragment in manifest['proposed'].items():
        path = writer / 'amendment-fragments' / (key + '.json')
        require(_load_json(path) == fragment, 'saved amendment preparation changed')
        prepared[key] = {'native_root': writer.relative_to(vault).as_posix(), 'preparation': pin(vault, path)}
    return {**manifest, 'revisions': prepared}


def recover(domain, request, *, preview):
    fields = IDENTITY | {'revise_fragments'} | (set() if preview else {'expected_manifest_sha256'})
    require(set(request) == fields, 'targeted writing revision requires an exact operator intent')
    ids = request['revise_fragments']
    require(isinstance(ids, list) and all(isinstance(key, str) and re.fullmatch(r'[a-f0-9]{64}', key) for key in ids)
            and len(set(ids)) == len(ids), 'targeted writing revision requires unique request IDs')
    run_id, round_id, op_id = (safe_id(request[key]) for key in ('run_id', 'round_id', 'operation_id'))
    control = IncrementalControl(domain.vault, run_id)
    root = domain.root(run_id)
    path = disposition_path(domain, run_id, round_id, op_id).with_name('manifest.json')
    with _exclusive_lock(root / '.controller.lock', timeout=0, marker=False):
        head = control.status()
        manifest = _load_json(path) if not preview and path.is_file() else make_plan(domain, request, head)
        digest = fingerprint(manifest)
        require(all(manifest[key] == request[key] for key in fields - {'expected_manifest_sha256', 'expected_revision'})
                and manifest['control_revision'] == request['expected_revision'], 'amendment operation changed inputs')
        summary = {'manifest_sha256': digest, 'round_id': round_id, 'decisions': manifest['decisions'],
                   'dispatched': len(manifest['dispatch_request_ids'])}
        if preview:
            builder = FileKnowledgeBuildService(domain.vault)
            frozen = domain.round_input(run_id, round_id)
            catalogue = catalogue_for_round(domain, run_id, frozen, builder)
            written, held, pages, cards, _, _ = evaluation(domain, builder, frozen, manifest, catalogue)
            carried = carried_sections(domain.vault, written)
            return {**summary, 'original_pages_ready': len(written) - len(carried) + len(pages),
                    'held_parent_pages': len(held),
                    'section_candidates': [change['name'] for change in carried] + [change['name'] for change, _ in cards]}
        require(digest == request['expected_manifest_sha256'], 'targeted writing preview no longer matches')
        save(path, manifest)
        pointer = pin(domain.vault, path)
        control.control({'run_id': run_id, 'actor': request['actor'], 'operation_id': op_id,
                         'expected_revision': request['expected_revision'], 'action': 'continue',
                         'boundary_fingerprint': request['boundary_fingerprint'], 'writing_amendment': pointer})
        head = control.status()
        require(head.get('writing_amendments', {}).get(round_id, {}).get('manifest') == pointer,
                'writing amendment lost its exact control authorization')
    from incremental_ingest_controller import resume_run

    controller = resume_run(domain, run_id)
    return {**summary, 'state': control.status()['state'], 'manifest': pointer, 'controller': controller}


def execute(domain, builder, frozen, authority, output):
    from incremental_runtime import execute_rows, initialize_scope

    pointer = authority['manifest']
    manifest = read_pin(domain.vault, pointer)
    require(manifest['contract'] == CONTRACT and manifest['frozen_input'] == frozen['input_fingerprint']
            and manifest['operation_id'] == authority['operation_id'], 'writing amendment lost its frozen inputs')
    run_id, round_id = frozen['run_id'], frozen['round_id']
    writer, operation = initialize_scope(domain, run_id, 'round', round_id, 'writing', frozen['snapshots'],
                                          recovery_id=manifest['operation_id'], amendment=pointer)
    for key, fragment in manifest['proposed'].items():
        save(writer / 'amendment-fragments' / (key + '.json'), fragment)
    rows = {part['request']['id']: part['request'] for group in manifest['proposed'].values() for part in group['fragments']}
    require(set(rows) == set(manifest['dispatch_request_ids']), 'amendment dispatch differs from its authorization')
    if rows:
        execute_rows(domain, run_id, writer, operation, [rows[key] for key in manifest['dispatch_request_ids']])
    catalogue = catalogue_for_round(domain, run_id, frozen, builder)
    bound = complete_manifest(domain, manifest, writer)
    existing, held, pages, cards, section_holds, summaries = evaluation(domain, builder, frozen, bound, catalogue)
    written = list(existing)
    for change, entries in [*pages, *cards]:
        path = writer / 'amendment-pages' / (change['change_id'] + '.json')
        save(path, page_record(change, entries, pointer))
        written.append({'preparation': pin(domain.vault, path), 'native_root': writer.relative_to(domain.vault).as_posix()})
    carried = carried_sections(domain.vault, existing)
    result = {'written': written, 'held': held, 'amendment': pointer, 'revisions': bound['revisions'],
              'original_pages': len(existing) - len(carried) + len(pages),
              'independent_sections': [*carried, *(change for change, _ in cards)],
              'section_holds': section_holds, 'fragments': summaries}
    save(output, result)
    return result


def page_record(change, entries, pointer):
    return {'contract': CONTRACT, 'change': change, 'manifest': pointer,
            'proofs': [{**row['proof'], **({'mechanical_review': row['review']} if 'review' in row else {})}
                       for row in entries]}


def formal_pages(domain, builder, frozen, catalogue, pointer, writer):
    """Re-verify the native receipts and deterministic candidates at Build ingress."""
    manifest = read_pin(domain.vault, pointer)
    bound = complete_manifest(domain, manifest, writer)
    _, _, pages, cards, _, _ = evaluation(domain, builder, frozen, bound, catalogue)
    return {change['change_id']: (page_record(change, entries, pointer), entries)
            for change, entries in [*pages, *cards]}
