"""Continue an authorized incremental writing revision from a drained manual stop."""
from collections import Counter
from pathlib import Path

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_pages import validate_page_revision
from hermes_source_units.incremental_sources import pinned, read_pin, safe_id
from hermes_source_units.knowledge_build import FileKnowledgeBuildService
from hermes_source_units.source_units import _exclusive_lock, _load_json, _sha, _vault_path
from hermes_source_units.validation import ContractError, canonical_json, fingerprint
from incremental_native import verified_result
from incremental_recovery import session_usage, stopped_history
from incremental_writing import (
    CURRENT_WRITING_REVISION, EvidenceCatalogue, assemble, catalogue_for_round, fragment_receipt, prepare_work,
)
from prepare_short_evidence_test import save
from short_evidence_pass import require

CONTRACT = 'incremental-writing-continuation/v1'
IDENTITY = {'run_id', 'round_id', 'actor', 'operation_id', 'expected_revision', 'run_revision', 'boundary_fingerprint'}


def pin(vault, path):
    return pinned(vault, path.relative_to(vault).as_posix())


def owned(fragment):
    return {canonical_json(ref) for ref in fragment['prepared']['support_refs']}


def required_reading(fragment):
    prepared = fragment['prepared']
    return {canonical_json(prepared['material_aliases'][row['ref']])
            for row in prepared['projection']['view']['materials']
            if row['role'] == 'core' or row.get('required_dependency')}


def match_slots(original, current, history):
    """Match authoring scope and isolate any required reading owned by an unknown attempt."""
    candidates = [(owned(part), part, history[part['request']['id']]['state']) for part in original]
    uncertain = set().union(*(refs for refs, _, state in candidates if state == 'isolated_attempt'))
    decisions = []
    for fragment in current:
        refs = owned(fragment)
        overlaps = [(old, part, state) for old, part, state in candidates if refs & old]
        prior = [part['request']['id'] for _, part, _ in overlaps]
        decision = {'request_id': fragment['request']['id'], 'prior_requests': prior}
        if (len(overlaps) == 1 and overlaps[0][0] == refs and overlaps[0][2] == 'valid'
                and overlaps[0][1]['prepared']['projection'] == fragment['prepared']['projection']):
            decision.update(disposition='reuse', prior_request=prior[0])
        elif required_reading(fragment) & uncertain:
            decision.update(disposition='held', reason='required reading overlaps an unconfirmed writing assignment')
        elif (overlaps and all(state == 'unattempted' for _, _, state in overlaps)
              and refs <= set().union(*(old for old, _, _ in overlaps))):
            decision.update(disposition='dispatch')
        else:
            decision.update(disposition='held', reason='scope has spent, held or newly assigned writing responsibilities')
        decisions.append(decision)
    return decisions


def fingerprinted_plan(domain, request, head):
    run_id, round_id = request['run_id'], request['round_id']
    run = domain.status(run_id)
    require(request['actor'] == run['actor'] == head['actor'] and run['state'] == 'active'
            and request['run_revision'] == run['revision'] and run['active_round_id'] == round_id
            and request['expected_revision'] == head['revision'] and head['state'] == 'paused'
            and head['boundary']['stage'] == 'manual' and not head['active_scopes']
            and request['boundary_fingerprint'] == fingerprint(head['boundary'])
            and 'writing' in head['pause_after'], 'writing recovery lost its exact manual stop')
    repair = head.get('writing_repairs', {}).get(round_id)
    require(repair and round_id not in head.get('writing_continuations', {}),
            'writing continuation needs the original authorized revision')
    root = domain.root(run_id)
    rr = root / 'rounds' / round_id
    writer = root / 'native/round' / round_id / 'writing/recoveries' / safe_id(repair['operation_id'])
    require(not any((rr / name).exists() for name in ('writing-revision.json', 'formal-request.json', 'delivery.json')),
            'writing recovery requires an interrupted revision before Build')
    prior_disposition = rr / 'writing-disposition.json'
    require(_sha(prior_disposition.read_bytes()) == repair['prior_disposition_sha256'],
            'original writing disposition changed')
    operation_path, stop = writer / 'operation.json', writer / 'stop.json'
    operation = _load_json(operation_path)
    frozen = domain.round_input(run_id, round_id)
    bound = operation['incremental']
    require(stop.is_file() and operation.get('workflow_mode') == 'incremental_v1'
            and Path(operation['vault']).resolve() == domain.vault and operation['actor'] == run['actor']
            and bound['run_id'] == run_id and bound['scope_kind'] == 'round' and bound['scope_id'] == round_id
            and bound['stage'] == 'writing' and bound['snapshots'] == frozen['snapshots'],
            'stopped writer does not match the authorized frozen round')
    builder = FileKnowledgeBuildService(domain.vault)
    catalogue = catalogue_for_round(domain, run_id, frozen, builder)
    changes_path = rr / 'page-changes.json'
    approved = {row['change_id']: row for row in _load_json(changes_path) if row['change_id'] in repair['plan_ids']}
    require(set(approved) == set(repair['plan_ids']), 'original authorization lost a page plan')
    originals = {}
    for path in sorted((writer / 'fragment-plans').glob('*.json')):
        work = _load_json(path)
        plan_id = work['plan_id']
        require(plan_id in approved and path.stem == plan_id, 'fragment plan escaped the original authorization')
        require(work == prepare_work(builder, catalogue, approved[plan_id],
                                     writing_revision=work.get('writing_revision', 1)),
                'historical fragment no longer matches its original qualified evidence')
        originals[plan_id] = (work, pin(domain.vault, path))
    require(originals, 'stopped writer has no bound fragment preparations')
    rows = [frag['request'] for work, _ in originals.values() for frag in work['fragments']]
    history = stopped_history(domain.vault, writer, operation, rows, run['reservations'])
    for work, _ in originals.values():
        for part in work['fragments']:
            row = part['request']
            entry = history[row['id']]
            if entry['state'] != 'acknowledged':
                continue
            if entry['native_state'] != 'settled':
                entry.update(state='held', reason='native writer held the original fragment')
                continue
            receipt_path = writer / 'receipts' / (row['id'] + '.json')
            entry['receipt'] = pin(domain.vault, receipt_path)
            try:
                validate_page_revision(part['prepared'], _load_json(receipt_path)['answer'])
            except ContractError as error:
                entry.update(state='held', reason=str(error))
            else:
                entry['state'] = 'valid'
    recorded_usage = session_usage(history)
    pages, new_rows = {}, []
    for plan_id, change in approved.items():
        if plan_id not in originals:
            continue
        old, pointer = originals[plan_id]
        page = pages.setdefault(plan_id, {'original_work': pointer, 'slots': []})
        try:
            current = prepare_work(builder, catalogue, change, writing_revision=CURRENT_WRITING_REVISION)
        except (ContractError, ValueError, OSError) as error:
            page['hold'] = str(error)
            continue
        slots = match_slots(old['fragments'], current['fragments'], history)
        page.update(work_sha256=fingerprint(current), slots=slots)
        new_rows.extend(part['request'] for part, decision in zip(current['fragments'], slots, strict=True)
                        if decision['disposition'] == 'dispatch')
    budget_allowance(run, new_rows)
    return {'contract': CONTRACT, 'run_id': run_id, 'actor': run['actor'], 'round_id': round_id,
            'operation_id': request['operation_id'], 'run_revision': run['revision'], 'control_revision': head['revision'],
            'boundary_fingerprint': request['boundary_fingerprint'], 'frozen_input': frozen['input_fingerprint'],
            'writing_revision': CURRENT_WRITING_REVISION, 'original_authorization': repair,
            'prior_disposition': pin(domain.vault, prior_disposition), 'changes': pin(domain.vault, changes_path),
            'previous_operation': pin(domain.vault, operation_path), 'stop': pin(domain.vault, stop),
            'history': history, 'prior_recorded_usage': recorded_usage, 'pages': pages,
            'dispatch_request_ids': [row['id'] for row in new_rows]}


def budget_allowance(run, rows):
    costs = {'max_calls': len(rows),
             'max_input_tokens': sum(row['budget'].get('text_tokens') or row['budget']['utf8_bytes'] for row in rows),
             'max_output_tokens': sum(row['output_cap'] for row in rows)}
    reservations = list(run['reservations'].values())
    prior = {r['request_fingerprint'] for r in reservations}
    ids = {fingerprint({'request': row['id'], 'ordinal': 1}) for row in rows}
    require(len(ids) == len(rows) and not ids & prior, 'recovery request has an existing consumption reservation')
    require(all(sum(r[limit] for r in reservations) + cost <= run['budget'][limit] for limit, cost in costs.items())
            and sum(r['max_calls'] for r in reservations if r['phase'] == 'writing') + len(rows)
            <= run['budget']['phase_limits']['writing'], 'writing continuation exceeds its original cumulative budget')


def recover(domain, request, *, preview=False):
    """Preview or apply an exact rebind of the original interrupted writing scope."""
    fields = IDENTITY if preview else IDENTITY | {'expected_manifest_sha256'}
    require(set(request) == fields, 'writing recovery requires an exact operator intent')
    run_id, round_id, operation_id = (safe_id(request[key]) for key in ('run_id', 'round_id', 'operation_id'))
    root = domain.root(run_id)
    control = IncrementalControl(domain.vault, run_id)
    with _exclusive_lock(root / '.controller.lock', timeout=0, marker=False):
        head = control.status()
        path = root / 'rounds' / round_id / 'writing-continuations' / (operation_id + '.json')
        if not preview and path.is_file():
            manifest = _load_json(path)
            require(fingerprint(manifest) == request['expected_manifest_sha256']
                    and all(manifest[key] == request[key] for key in fields - {'expected_manifest_sha256', 'expected_revision'})
                    and manifest['control_revision'] == request['expected_revision'],
                    'writing recovery operation identifies different inputs')
        else:
            manifest = fingerprinted_plan(domain, request, head)
        digest = fingerprint(manifest)
        summary = {'manifest_sha256': digest, 'round_id': round_id, 'history': dict(Counter(
                       item['state'] for item in manifest['history'].values())),
                   'slots': dict(Counter(slot['disposition'] for page in manifest['pages'].values()
                                         for slot in page['slots'])),
                   'prior_recorded_usage': manifest['prior_recorded_usage']}
        if preview:
            return summary
        require(digest == request['expected_manifest_sha256'], 'writing recovery preview no longer matches the confirmed scope')
        save(path, manifest)
        pointer = pin(domain.vault, path)
        control.control({'run_id': run_id, 'actor': request['actor'], 'operation_id': operation_id,
                         'expected_revision': request['expected_revision'], 'action': 'continue',
                         'boundary_fingerprint': request['boundary_fingerprint'], 'writing_continuation': pointer})
        active = control.status()
        require(active.get('writing_continuations', {}).get(round_id, {}).get('manifest') == pointer,
                'writing continuation control receipt is missing')
    if active['state'] != 'active':
        return {**summary, 'state': active['state']}
    from incremental_ingest_controller import attach_sources, spawn

    attach_sources(domain, run_id)
    return {**summary, 'state': 'active', 'controller': spawn(domain.vault, run_id), 'manifest': pointer}


def continued_answers(vault, writer, work, continuation):
    """Check a newly authored fragment or its exact authenticated old receipt for Build."""
    manifest = read_pin(vault, continuation)
    page = manifest['pages'][work['plan_id']]
    require(page['work_sha256'] == fingerprint(work), 'continued fragment plan changed')
    old_writer = _vault_path(vault, manifest['previous_operation']['ref']).parent
    originals = {part['request']['id']: part for part in read_pin(vault, page['original_work'])['fragments']}
    answers, proofs = [], []
    for fragment, slot in zip(work['fragments'], page['slots'], strict=True):
        require(fragment['request']['id'] == slot['request_id'], 'continued request differs from its selected slot')
        if slot['disposition'] == 'reuse':
            prior = slot['prior_request']
            part = originals[prior]
            require(manifest['history'][prior]['state'] == 'valid' and owned(part) == owned(fragment)
                    and part['prepared']['projection'] == fragment['prepared']['projection'],
                    'reused receipt failed the original evidence and ownership checks')
            answer, proof = fragment_receipt(vault, old_writer, part)
            require(proof['receipt'] == manifest['history'][prior]['receipt'],
                    'reused native receipt changed after recovery planning')
            validate_page_revision(part['prepared'], answer)
            proof['continued_request_id'] = slot['request_id']
        else:
            require(slot['disposition'] == 'dispatch', 'page has an isolated writing fragment')
            answer, proof = fragment_receipt(vault, writer, fragment)
        answers.append(answer)
        proofs.append(proof)
    return answers, proofs


def execute_continuation(domain, frozen, changes, builder, maps, authority, path):
    """Settle original known usage, dispatch only authorized requests, then stage eligible pages."""
    from incremental_runtime import execute_rows, initialize_scope, settle_usage

    vault = domain.vault
    pointer = authority['manifest']
    manifest = read_pin(vault, pointer)
    require(manifest['contract'] == CONTRACT and manifest['frozen_input'] == frozen['input_fingerprint']
            and manifest['operation_id'] == authority['operation_id'], 'writing continuation inputs changed')
    run_id, round_id = manifest['run_id'], manifest['round_id']
    old_op = read_pin(vault, manifest['previous_operation'])
    read_pin(vault, manifest['stop'])
    require(read_pin(vault, manifest['changes']) == changes, 'continued round lost its original page plans')
    old_writer = _vault_path(vault, manifest['previous_operation']['ref']).parent
    writer, operation = initialize_scope(domain, run_id, 'round', round_id, 'writing', frozen['snapshots'],
                                         recovery_id=manifest['operation_id'], continuation=pointer)
    require(old_op['incremental'] == operation['incremental'], 'continued writer changed the original stage identity')
    catalogue = EvidenceCatalogue(builder, maps, operation['actor'])
    prior = read_pin(vault, manifest['prior_disposition'])
    written, held = list(prior['written']), [row for row in prior['held'] if row['change_id'] not in manifest['pages']]
    prepared, rows = [], []
    for plan in changes:
        page = manifest['pages'].get(plan['change_id'])
        if page is None:
            continue
        original = read_pin(vault, page['original_work'])
        for part in original['fragments']:
            row = part['request']
            entry = manifest['history'][row['id']]
            for attempt in entry['attempts']:
                for field in ('attempt', 'audit'):
                    if field in attempt:
                        read_pin(vault, attempt[field])
            if 'proof' in entry:
                result, proof = verified_result(old_writer, row)
                require(proof == entry['proof'], 'original completion changed before continuation')
                settle_usage(domain, run_id, writer, row, result)
        if 'hold' in page:
            held.append({'change_id': plan['change_id'], 'page_id': plan['page_id'], 'state': 'held', 'reason': page['hold']})
            continue
        work = prepare_work(builder, catalogue, plan, writing_revision=manifest['writing_revision'])
        require(fingerprint(work) == page['work_sha256'], 'current scope differs from the authorized writing continuation')
        prep = writer / 'fragment-plans' / (plan['change_id'] + '.json')
        save(prep, work)
        prepared.append((plan, page, work, prep))
        rows.extend(fragment['request'] for fragment, slot in zip(work['fragments'], page['slots'], strict=True)
                    if slot['disposition'] == 'dispatch' and fragment['request']['id'] == slot['request_id'])
    require([row['id'] for row in rows] == manifest['dispatch_request_ids'], 'continued dispatch differs from its authorization')
    if rows:
        execute_rows(domain, run_id, writer, operation, rows)
    for plan, page, work, prep in prepared:
        try:
            answers, _ = continued_answers(vault, writer, work, pointer)
            assemble(builder, work, answers)
        except (ContractError, ValueError, OSError) as error:
            reason = next((slot['reason'] for slot in page['slots'] if slot['disposition'] == 'held'), str(error))
            held.append({'change_id': plan['change_id'], 'page_id': plan['page_id'], 'state': 'held', 'reason': reason})
        else:
            written.append({'preparation': pin(vault, prep), 'native_root': writer.relative_to(vault).as_posix()})
    outcome = {'written': written, 'held': held, 'repair': manifest['original_authorization'],
               'writing_continuation': pointer}
    save(path, outcome)
    return outcome
