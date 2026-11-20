"""Recoverable native scheduling for explicit fixed-source incremental runs."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import pinned, read_pin
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import NativeCompletionUnconfirmed, mutate, service, verified_result
from incremental_runtime import (execute_rows, initialize_scope, map_source, request_path, source_alias,
                                 source_pin)
from short_evidence_pass import require, request as semantic_request


def spawn(vault, run_id):
    domain = service(Path(vault), run_id)
    root = domain.root(run_id)
    path = root / 'controller.json'
    if path.is_file():
        state = _load_json(path)
        proc = Path('/proc') / str(state['pid'])
        if proc.is_dir() and state.get('start') == (proc / 'stat').read_text().split(') ', 1)[1].split()[19]:
            return state
    script = Path(__file__).resolve().parents[1] / 'scripts/manage_incremental_ingest.py'
    with (root / 'controller.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-B', str(script), '--vault', str(vault),
                                    'watch', '--run-id', run_id], stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    start = (Path('/proc') / str(process.pid) / 'stat').read_text().split(') ', 1)[1].split()[19]
    state = {'pid': process.pid, 'start': start}
    _write_atomic(path, _json_bytes(state))
    return state


def sync_sources(domain, run_id):
    import run_short_evidence_test as native

    run = domain.status(run_id)
    wid = run['source_workflow_id']
    workflow = _load_json(_vault_path(domain.vault, f'_system/ledgers/ingest-workflows/{wid}.json'))
    require(not workflow['cancel_requested'] and workflow['actor'] == run['actor']
            and workflow['scope'].get('workflow_mode') == 'incremental_v1'
            and workflow['scope'].get('incremental_run_id') == run_id
            and set(workflow['scope']['source_paths']) == {row['path'] for row in run['source_manifest']},
            'source workflow stopped or fixed incremental binding changed')
    kb, kc, _ = native.runtime()
    for outcome in workflow['source_outcomes']:
        require(outcome['path'] in run['sources'], 'source outcome escaped the fixed incremental manifest')
        if run['sources'][outcome['path']]['state'] != 'pending':
            continue
        node = 'source-prepare:' + fingerprint({'path': outcome['path'], 'content_sha256': outcome['content_sha256']})[:16]
        card = next((row for row in workflow['kanban']['task_map'] if row['node'] == node), None)
        if card is None:
            continue  # Durable domain output alone is not trusted native completion.
        with closing(kc.connect(board=workflow['kanban']['board_id'])) as connection:
            task = kb.get_task(connection, card['task_id'])
            runs = kb.list_runs(connection, card['task_id'], include_active=False)
        if task is None or task.status not in ('done', 'archived') or task.claim_lock:
            continue
        ended = next((row for row in reversed(runs) if row.outcome == 'completed'), None)
        if not ended:
            continue
        proof = {'board': workflow['kanban']['board_id'], 'task_id': task.id, 'run_id': ended.id}
        action = 'seal_source_snapshot' if outcome['status'] == 'ready' else 'record_source_failure'
        fields = {'source_path': outcome['path'], 'outcome': outcome}
        if outcome['status'] == 'ready':
            fields['native_completion'] = proof
        mutate(domain, run_id, action, 'source-' + fingerprint(outcome)[:56], **fields)
    return domain.status(run_id)


def attach_sources(domain, run_id):
    """Reattach the original source coordinator without resetting its outcomes."""
    from hermes_source_units import FileIngestWorkflowService
    from orchestration import _mutation, dispatch
    from start_vault_workflow import start as provision
    from watcher_runtime import confirm_handoff, coordinator_pid

    control = IncrementalControl(domain.vault, run_id)
    with _exclusive_lock(domain.root(run_id) / '.source-attach.lock'):
        run = domain.status(run_id)
        control.check_admission({'run_id': run_id, 'actor': run['actor'], 'scope_kind': 'run',
                                 'scope_id': run_id, 'stage': 'prepare',
                                 'input_fingerprint': run['start_fingerprint']})
        workflow_service = FileIngestWorkflowService(domain.vault)
        if not workflow_service._path(run['source_workflow_id']).exists():
            runtime = _load_json(request_path(domain, run_id))
            provision(runtime['source_request'], runtime['record'], runtime['bootstrap_skill'],
                      incremental_bootstrap=True)
        workflow = workflow_service.status(run['source_workflow_id'])
        require(workflow['actor'] == run['actor'] and not workflow['cancel_requested']
                and workflow['state'] != 'failed'
                and workflow['scope'].get('workflow_mode') == 'incremental_v1'
                and workflow['scope'].get('incremental_run_id') == run_id,
                'original source workflow is stopped or its binding changed')
        require(not workflow_service.pending_pause(workflow), 'original source workflow has a durable pause')
        if not workflow_service.source_coverage(workflow)['pending']:
            return {'state': 'sources_settled', 'workflow_id': workflow['workflow_id']}
        pid = coordinator_pid(domain.vault, workflow['workflow_id'])
        if pid:
            return {'state': 'attached', 'workflow_id': workflow['workflow_id'], 'reconciler_pid': pid}
        control.check_admission({'run_id': run_id, 'actor': run['actor'], 'scope_kind': 'run',
                                 'scope_id': run_id, 'stage': 'prepare',
                                 'input_fingerprint': run['start_fingerprint']})
        result = dispatch(domain.vault, 'resume', _mutation(workflow))
        if result.get('background_dispatch') or result.get('initial_release_pending'):
            result['handoff'] = confirm_handoff(domain.vault, workflow['workflow_id'], result.get('reconciler_pid'))
        return result


def bind_request(spec, estimate=4096, images=()):
    return semantic_request(spec['phase'], spec['view'], spec['schema'], estimate, 40000, images)


def verified_page_changes(service, frozen, aliases, hydrated, coordinator, row, plan_path):
    """Recover decisions from the exact native prompt and acknowledged response."""
    from hermes_source_units.incremental_pages import normalize_page_changes
    from prepare_short_evidence_test import save

    require(row['phase'] == 'incremental-page-plan' and row['view']['round'] == frozen
            and row['view']['mapping_results'] == hydrated,
            'coordination request differs from the frozen round')
    result, _ = verified_result(coordinator, row)
    require(result['state'] == 'settled', 'coordination requires its verified native completion')
    answer = _load_json(coordinator / 'receipts' / (row['id'] + '.json'))['answer']
    changes = normalize_page_changes(service, frozen, answer, aliases,
                                     registered_pages=row['view']['registered_pages'])
    save(plan_path, changes)
    return changes


def acknowledged_coordination(root, row):
    if (root / 'jobs' / (row['id'] + '.result.json')).is_file():
        try:
            return verified_result(root, row)[0]
        except NativeCompletionUnconfirmed:
            pass
    return None


def grouped_page_changes(domain, run_id, service, frozen, aliases, hydrated, coordinator, operation, root):
    """Resume exact, bounded native decisions over complete topic scopes."""
    from hermes_source_units.incremental_pages import (coordination_catalogue, group_aliases,
                                                       normalize_page_changes, plan_page_changes, registry_view)
    from incremental_writing_evidence import EvidenceCatalogue, normalized
    from incremental_coordination_repair import (corrected_changes, corrected_request, eligible_targets, has_budget)
    from prepare_short_evidence_test import save

    manifest_path = root / 'coordination-groups.json'
    saved = _load_json(manifest_path) if manifest_path.exists() else None
    registry = saved['registered_pages'] if saved else registry_view(service)
    revision = saved.get('planning_revision', 1) if saved else 4
    catalogue = coordination_catalogue(frozen, hydrated)
    inventory = EvidenceCatalogue(service, hydrated, operation['actor']) if revision >= 3 else None
    groups = []

    def partition(topics):
        spec = plan_page_changes(service, frozen, hydrated, topics=topics, registered_pages=registry,
                                planning_revision=min(revision, 2))
        mandatory = group_aliases(topics)
        offered = set(mandatory)
        if inventory:
            names = {normalized(topic['name']) for topic in topics}
            context = []
            for mid, snapshot, tid, topic in inventory.topics:
                if mid not in frozen.get('context_map_result_ids', []) or normalized(topic['name']) not in names:
                    continue
                refs = sorted({key for link in topic['relations']
                               for key in [link['item'], *link['applies_to']]
                               if inventory.aliases[key]['coverage'] == 'mapped'})
                offered.update(refs)
                context.append({'topic_id': tid, 'snapshot_id': snapshot, 'name': topic['name'],
                                'scope': topic['scope'], 'aliases': refs})
            spec['view'].update(planning_revision=revision, context_support=context,
                                support_material={key: inventory.label(key) for key in sorted(offered)})
            spec['view']['instructions'] += (
                ' Choose registered source text that supports the planned requirements and its linked conditions '
                'and tables. Navigation and titles only locate material. Context support is optional, and may be '
                'selected only for a compatible page purpose and applicability scope. A matching topic name alone '
                'cannot establish compatibility. The program binds and verifies original material before writing.')
        if revision >= 4:
            spec['view']['instructions'] += (
                ' Propose a page when the sealed Map supports its scope. The writer checks the full original and '
                'necessary assets later. If a concrete evidence dependency blocks planning, identify it in the hold reason. '
                'If a hold only awaits the later writing check, start its reason with writing_review_pending.')
        request = bind_request(spec)
        if not request['budget']['fits'] and len(topics) > 1:
            half = len(topics) // 2
            partition(topics[:half])
            partition(topics[half:])
        else:
            group = {'request': request, 'aliases': sorted(offered)}
            if inventory:
                group['required_aliases'] = mandatory
            groups.append(group)

    if catalogue:
        partition(catalogue)
    manifest = {'contract': 'incremental-coordination-groups/v1', 'round': frozen['input_fingerprint'],
                'map_inputs_sha256': fingerprint(hydrated), 'registered_pages': registry, 'groups': groups}
    if revision != 1:
        manifest['planning_revision'] = revision
    save(manifest_path, manifest)
    changes, gaps, repairs = [], [], []
    rows = [group['request'] for group in groups if group['request']['budget']['fits']]
    plan_path = root / 'page-changes.json'
    if rows and not plan_path.exists():
        execute_rows(domain, run_id, coordinator, operation, rows)
    for group in groups:
        row = group['request']
        group_refs = {alias: aliases[alias] for alias in group['aliases']}
        if not row['budget']['fits']:
            gaps.append({'request_id': row['id'], 'aliases': list(group_refs),
                         'reason': 'complete topic scope exceeds the coordination request budget'})
            continue
        result = acknowledged_coordination(coordinator, row) if revision >= 4 else verified_result(coordinator, row)[0]
        if result is None:
            gaps.append({'request_id': row['id'], 'aliases': list(group_refs),
                         'reason': 'native coordination completion is unconfirmed'})
            continue
        if result['state'] != 'settled':
            gaps.append({'request_id': row['id'], 'aliases': list(group_refs),
                         'reason': 'native coordination held this topic group'})
            continue
        answer = _load_json(coordinator / 'receipts' / (row['id'] + '.json'))['answer']
        assigned, held = set(), {}
        for position, raw in enumerate(answer['changes'], 1):
            try:
                change, = normalize_page_changes(service, frozen, {'changes': [raw]}, group_refs,
                                                registered_pages=registry)
                changes.append(change)
                assigned.update(raw['support_aliases'])
                if change['action'] == 'hold':
                    held[str(position)] = raw, change
            except (ContractError, ValueError) as error:
                gaps.append({'request_id': row['id'], 'proposal_sha256': fingerprint(raw), 'reason': str(error)})
        if missing := sorted(set(group.get('required_aliases', group_refs)) - assigned):
            gaps.append({'request_id': row['id'], 'aliases': missing,
                         'reason': 'native coordination did not assign these aliases to a page or hold'})
        if revision < 4 or not held or len(result.get('attempts', [])) != 1 or result['attempts'][0]['state'] != 'returned':
            continue
        approval_path = coordinator / 'coordination-repairs' / (row['id'] + '.json')
        if approval_path.is_file():
            approval = _load_json(approval_path)
            targets = approval['targets']
            require(all(slot in held and fingerprint(held[slot][0]) == target['hold_sha256']
                        for slot, target in targets.items()), 'saved repair targets differ from their original holds')
        elif plan_path.exists():
            continue
        else:
            with service.source.reading_snapshot():
                targets = eligible_targets(held, inventory, set(group_refs))
        if not targets:
            continue
        corrected = corrected_request(row, answer, targets)
        save(approval_path, {'targets': targets, 'request_id': corrected['id']})
        repairs.append((row['id'], corrected, targets, held, group_refs))
    if repairs:
        repaired_rows = [row for _, row, *_ in repairs if row['budget']['fits']]
        dispatched = ({row['id'] for row in has_budget(domain.status(run_id), repaired_rows)}
                      if not plan_path.exists() else set(_load_json(root / 'coordination-repair-outcomes.json')['dispatched']))
        if not plan_path.exists() and dispatched:
            execute_rows(domain, run_id, coordinator, operation, [row for row in repaired_rows if row['id'] in dispatched])
        occupied = ({row['page_id'] for row in changes if row['action'] != 'hold'},
                    {row['path'].casefold() for row in changes if row['action'] != 'hold'})
        outcomes, replaced, recovered = [], set(), []
        for prior, row, targets, held, group_refs in repairs:
            accepted = {}
            status = 'over_budget' if not row['budget']['fits'] else 'budget_unavailable'
            if row['id'] in dispatched:
                result = acknowledged_coordination(coordinator, row)
                status = 'native_completion_unconfirmed'
                if result is not None:
                    status = 'native_repair_held'
                    if result['state'] == 'settled':
                        answer = _load_json(coordinator / 'receipts' / (row['id'] + '.json'))['answer']
                        accepted = corrected_changes(service, frozen, answer, targets, group_refs, registry, occupied)
                        status = 'evaluated'
            replaced.update(held[slot][1]['change_id'] for slot in accepted)
            recovered.extend(accepted.values())
            outcomes.append({'parent_request_id': prior, 'request_id': row['id'], 'state': status,
                             'accepted': {slot: change['change_id'] for slot, change in accepted.items()},
                             'retained_hold_ids': [held[slot][1]['change_id'] for slot in sorted(targets, key=int)
                                                   if slot not in accepted]})
        save(root / 'coordination-repair-outcomes.json', {'dispatched': sorted(dispatched), 'outcomes': outcomes})
        changes = [row for row in changes if row['change_id'] not in replaced] + recovered
    # A duplicate decision is safe to deduplicate. Distinct competing decisions
    # must not overwrite one another or be merged by the mechanical scheduler.
    changes = list({change['change_id']: change for change in changes}.values())
    ids = Counter(change['page_id'] for change in changes if change['action'] != 'hold')
    paths = Counter(change['path'].casefold() for change in changes if change['action'] != 'hold')
    independent = []
    for change in changes:
        if change['action'] != 'hold' and (ids[change['page_id']] > 1 or paths[change['path'].casefold()] > 1):
            gaps.append({'change': change, 'reason': 'conflicting native decisions target the same page identity or path'})
        else:
            independent.append(change)
    save(root / 'coordination-gaps.json', gaps)
    save(plan_path, independent)
    return independent, gaps


def round_work(domain, run_id, round_id):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save
    from hermes_source_units.knowledge_build import FileKnowledgeBuildService
    from hermes_source_units.page_governance import POLICY
    from hermes_source_units.vault_finalize import FileVaultFinalizeService
    from incremental_writing import selected_change, writing_disposition
    from incremental_recovery import pending_request

    control = IncrementalControl(domain.vault, run_id)
    frozen = domain.round_input(run_id, round_id)
    scope = {'scope_kind': 'round', 'scope_id': round_id}
    root = domain.root(run_id) / 'rounds' / round_id
    maps = [domain.read_map(run_id, mid) for mid in [*frozen['map_result_ids'], *frozen.get('context_map_result_ids', [])]]
    aliases = {alias: {'unit_ref': row['unit_ref'], 'span': None} for mapped in maps for alias, row in mapped['alias_table'].items()}
    hydrated = [{**mapped, 'mapped': read_pin(domain.vault, mapped['results'][0])} for mapped in maps]
    build_service = FileKnowledgeBuildService(domain.vault)
    coordinator, operation = initialize_scope(domain, run_id, 'round', round_id, 'coordination', frozen['snapshots'])
    plan_path = root / 'page-changes.json'
    coordination_request_path = root / 'coordination-request.json'
    coordination_gaps = []
    with control.executing(scope, 'coordination', frozen['input_fingerprint']):
        if coordination_request_path.exists():
            row = _load_json(coordination_request_path)
            if not plan_path.exists():
                execute_rows(domain, run_id, coordinator, operation, [row])
            changes = verified_page_changes(build_service, frozen, aliases, hydrated, coordinator, row, plan_path)
        else:
            require(not plan_path.exists() or (root / 'coordination-groups.json').is_file(),
                    'saved page changes lack their original coordination requests')
            changes, coordination_gaps = grouped_page_changes(domain, run_id, build_service, frozen, aliases,
                                                                hydrated, coordinator, operation, root)
    if control.checkpoint(scope, 'coordination', {'round': frozen, 'changes_sha256': native.sha(plan_path)})['state'] != 'active':
        return
    disposition_path, disposition = writing_disposition(domain, control, scope, frozen, changes, build_service, hydrated)
    written, held = disposition['written'], disposition['held']
    if control.checkpoint(scope, 'writing', {'writing_disposition_sha256': native.sha(disposition_path)})['state'] != 'active':
        return
    if not written:
        reused = bool(changes) and not held and not coordination_gaps
        reason = 'all planned pages reused' if reused else 'no selected page changes passed writing'
        return settle_held(domain, run_id, round_id, frozen, reason, reused=reused, gaps=[*held, *coordination_gaps])
    formal, formal_operation = initialize_scope(domain, run_id, 'round', round_id, 'formal-build', frozen['snapshots'])
    finalize = FileVaultFinalizeService(domain.vault)
    selected = [selected_change(domain.vault, row) for row in written]
    auth_path = root / 'build-authorization.json'
    authorization = {'contract': 'incremental-build-authorization/v1', 'run_id': run_id, 'round_id': round_id,
        'actor': domain.status(run_id)['actor'], 'pages': [change['change_id'] for change in selected],
        'page_changes': selected, 'base_release': finalize.status()['current_release_id'],
        'publish': True, 'policy': POLICY, 'input_fingerprint': frozen['input_fingerprint'],
        'source_snapshots': frozen['snapshots'], 'map_result_ids': frozen['map_result_ids']}
    policy = domain.status(run_id)['policy']
    changes_applied = any(
        path.is_file() and _load_json(path).get('state') == 'committed'
        and _load_json(path).get('source_changes') == policy.get('source_changes')
        for path in (domain.root(run_id) / 'rounds').glob('*/delivery.json'))
    if policy.get('source_changes') and not changes_applied:
        authorization.update(source_changes=policy['source_changes'], authorized_removed_pages=policy['authorized_removed_pages'])
    if policy.get('isolation_pages'):
        require(set(policy['isolation_pages']) <= {change['page_id'] for change in selected},
                'isolation permission must name pages selected in this exact round')
        authorization['isolation_pages'] = policy['isolation_pages']
    if auth_path.exists():
        authorization = _load_json(auth_path)
    else:
        save(auth_path, authorization)
    runtime = _load_json(request_path(domain, run_id))
    view = {'authorization': authorization, 'written': written, 'excluded': held,
            'delivery_id': 'delivery-' + fingerprint([run_id, round_id])[:40],
            'provider_config': runtime['provider_config'],
            'provider_config_sha256': native.sha(Path(runtime['provider_config']))}
    pause_stages = control.status()['pause_after']
    view['stop_after'] = ('build_committed' if 'formal-build' in pause_stages
                          else 'index_verified' if set(pause_stages) & {'release', 'acceptance'} else None)
    row = {'id': fingerprint(view), 'phase': 'incremental-formal-build', 'view': view}
    with control.executing(scope, 'formal-build', frozen['input_fingerprint']):
        saved_request = root / 'formal-request.json'
        if saved_request.exists():
            row = _load_json(saved_request)
        else:
            save(saved_request, row)
        job = formal / 'jobs' / (row['id'] + '.json')
        if pending_request(formal, formal_operation, row):
            native.stage(formal, formal_operation, [row], 1)
        result = _load_json(job.with_suffix('.result.json'))
        require(result['state'] == 'settled', 'incremental delivery remains incomplete')
    delivery_state = _load_json(root / 'delivery.json')
    if delivery_state['state'] == 'build_committed':
        if control.checkpoint(scope, 'formal-build', {'build': delivery_state['build_run_id']})['state'] != 'active':
            return
        recover_delivery(domain, run_id, round_id, 'after-build', stop_after='index_verified'
                         if set(pause_stages) & {'release', 'acceptance'} else None)
        delivery_state = _load_json(root / 'delivery.json')
    if delivery_state['state'] == 'index_verified':
        evidence = {'release': delivery_state['release_id'], 'hash': delivery_state['release_hash'],
                    'generation': delivery_state['provider']['index_generation']}
        for stage in ('release', 'acceptance'):
            if control.checkpoint(scope, stage, evidence)['state'] != 'active':
                return
        recover_delivery(domain, run_id, round_id, 'after-index')
        delivery_state = _load_json(root / 'delivery.json')
    require(delivery_state['state'] == 'committed', 'incremental delivery requires supported prefix recovery')
    return mutate(domain, run_id, 'settle_round', 'settled-' + round_id, round_id=round_id,
                  disposition={'round_input_fingerprint': frozen['input_fingerprint'], 'state': 'delivered',
                    'held_changes': [*held, *coordination_gaps],
                    'delivery': pinned(domain.vault, (root / 'delivery.json').relative_to(domain.vault).as_posix())})


def settle_held(domain, run_id, round_id, frozen, reason, *, reused=False, gaps=()):
    return mutate(domain, run_id, 'settle_round', 'settled-' + round_id, round_id=round_id,
                  disposition={'round_input_fingerprint': frozen['input_fingerprint'],
                               'state': 'reused' if reused else 'held', 'reason': reason,
                               **({'held_changes': list(gaps)} if gaps else {})})


def recover_delivery(domain, run_id, round_id, operation_id, *, stop_after=None, disposition='continue',
                     isolation=None, settle_terminal=False):
    import run_short_evidence_test as native
    from incremental_recovery import pending_request

    safe = __import__('hermes_source_units.incremental_sources', fromlist=['safe_id']).safe_id
    safe(operation_id)
    require(stop_after in (None, 'build_committed', 'index_verified')
            and disposition in ('continue', 'abort_uncommitted', 'isolate_committed'),
            'unknown incremental delivery recovery intent')
    require(disposition != 'abort_uncommitted' or stop_after is None,
            'aborting an uncommitted window cannot request a later checkpoint')
    require((disposition == 'isolate_committed' and isinstance(isolation, list) and isolation)
            or (disposition != 'isolate_committed' and not isolation),
            'forward isolation requires its exact reviewed page rows')
    frozen = domain.round_input(run_id, round_id)
    round_root = domain.root(run_id) / 'rounds' / round_id
    original = _load_json(round_root / 'formal-request.json')
    first = domain.root(run_id) / 'native' / 'round' / round_id / 'formal-build' / 'jobs' / (original['id'] + '.json')
    view = {**original['view'],
            'recovery': {'operation_id': operation_id,
                         'original_job': pinned(domain.vault, first.relative_to(domain.vault).as_posix()),
                         'disposition': disposition, 'stop_after': stop_after,
                         **({'isolation': isolation} if isolation else {})}}
    row = {'id': fingerprint(view), 'phase': 'incremental-formal-build', 'view': view}
    root, operation = initialize_scope(domain, run_id, 'round', round_id, 'formal-build',
                                       frozen['snapshots'], recovery_id=operation_id)
    control = IncrementalControl(domain.vault, run_id)
    if pending_request(root, operation, row):
        require(domain.status(run_id)['active_round_id'] == round_id,
                'delivery recovery must target the current frozen round')
        with control.executing({'scope_kind': 'round', 'scope_id': round_id}, 'formal-build', frozen['input_fingerprint']):
            native.stage(root, operation, [row], 1)
    result, _ = verified_result(root, row)
    require(result['state'] == 'settled', 'delivery recovery did not settle its native request')
    output = result['delivery']
    if settle_terminal and output['state'] in ('committed', 'aborted'):
        held = list(original['view']['excluded'])
        gap_path = round_root / 'coordination-gaps.json'
        if gap_path.exists():
            held.extend(_load_json(gap_path))
        if output.get('isolation'):
            isolated = read_pin(domain.vault, output['isolation'])['pages']
            changes = {row['page_id']: row for row in output['authorization']['page_changes']}
            held.extend({'change_id': changes[row['page_id']]['change_id'], 'page_id': row['page_id'],
                         'state': 'held', 'reason': row['reason']} for row in isolated)
        settlement = {'round_input_fingerprint': frozen['input_fingerprint'],
                      'state': 'delivered' if output['state'] == 'committed' else 'held',
                      'held_changes': held,
                      'delivery': pinned(domain.vault, (round_root / 'delivery.json').relative_to(domain.vault).as_posix())}
        if output['state'] == 'aborted':
            settlement['reason'] = 'Explicitly aborted the verified uncommitted delivery window'
            represented = {row.get('change_id') for row in held}
            settlement['held_changes'].extend(
                {'change_id': change['change_id'], 'page_id': change['page_id'], 'state': 'held',
                 'reason': settlement['reason']} for change in output['authorization']['page_changes']
                if change['change_id'] not in represented)
        mutate(domain, run_id, 'settle_round', 'settled-' + round_id, round_id=round_id, disposition=settlement)
    return output


def watch(vault, run_id):
    from orchestration import configuration

    domain = service(Path(vault), run_id)
    control = IncrementalControl(domain.vault, run_id)
    with _exclusive_lock(domain.root(run_id) / '.controller.lock', timeout=0):
        control.reconcile_executors()
        head = control.status()
        if head['state'] == 'active' and 'mapping' in head.get('pause_after', []):
            # A worker may have committed a Map while the old controller was
            # draining another failed future. Restore its checkpoint before dispatch.
            run = domain.status(run_id)
            for map_id in run['map_result_ids']:
                if map_id in run['consumed_map_ids']:
                    continue
                source = next(row for row in run['sources'].values() if row.get('map_result_id') == map_id)
                snapshot = source['snapshot_id']
                scope = {'scope_kind': 'source', 'scope_id': snapshot}
                evidence = {'snapshot_id': snapshot}
                boundary = {'scope': scope, 'stage': 'mapping', 'input_fingerprint': fingerprint(evidence),
                            'evidence': evidence}
                if fingerprint(boundary) not in head['released']:
                    require(domain.read_map(run_id, map_id)['snapshot_id'] == snapshot,
                            'saved Map differs from its source checkpoint')
                    if control.checkpoint(scope, 'mapping', evidence)['state'] != 'active':
                        break
        running = {}
        round_future = None
        with ThreadPoolExecutor(max_workers=configuration()['source_prepare_concurrency']) as pool, \
                ThreadPoolExecutor(max_workers=1) as rounds:
            while domain.status(run_id)['state'] == 'active':
                if round_future is not None and round_future.done():
                    round_future.result()
                    round_future = None
                if control.status()['state'] in ('paused', 'cancelled'):
                    return domain.status(run_id)
                if control.status()['state'] == 'pausing':
                    control.checkpoint({'scope_kind': 'run', 'scope_id': run_id}, 'mapping', {})
                    if control.status()['state'] == 'paused':
                        return domain.status(run_id)
                for snapshot, future in list(running.items()):
                    if future.done():
                        future.result()
                        del running[snapshot]
                        control.checkpoint({'scope_kind': 'source', 'scope_id': snapshot}, 'mapping',
                                           {'snapshot_id': snapshot})
                if control.status()['state'] != 'active':
                    time.sleep(1)
                    continue
                run = sync_sources(domain, run_id)
                for source in run['sources'].values():
                    if source['state'] == 'prepared' and source['snapshot_id'] not in running:
                        if len(running) >= configuration()['source_prepare_concurrency']:
                            break
                        running[source['snapshot_id']] = pool.submit(map_source, str(domain.vault), run_id, source['snapshot_id'])
                run = domain.status(run_id)
                if round_future is not None:
                    pass  # Keep sealing and mapping later-ready sources during this one fixed round.
                elif run['active_round_id']:
                    round_future = rounds.submit(round_work, domain, run_id, run['active_round_id'])
                elif any(mid not in run['consumed_map_ids'] for mid in run['map_result_ids']):
                    round_id = 'round-' + str(len(run['round_ids']) + 1)
                    try:
                        mutate(domain, run_id, 'freeze_round', 'freeze-' + round_id, round_id=round_id,
                               max_maps=run['policy']['max_maps_per_round'])
                    except ContractError as error:
                        if error.code != 'ROUND_BUSY':
                            raise
                elif not running and all(row['state'] in ('mapped', 'failed') for row in run['sources'].values()):
                    if control.checkpoint({'scope_kind': 'run', 'scope_id': run_id}, 'closure',
                                          {'maps': run['map_result_ids'], 'rounds': run['round_ids']})['state'] != 'active':
                        return run
                    return close(domain, run_id)
                time.sleep(1)
    return domain.status(run_id)


def closure_inputs(domain, run):
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    run_id = run['run_id']
    require(run['active_round_id'] is None and all(row['state'] in ('mapped', 'failed') for row in run['sources'].values())
            and set(run['map_result_ids']) == set(run['consumed_map_ids'])
            and all((domain.root(run_id) / 'rounds' / rid / 'settlement.json').is_file() for rid in run['round_ids']),
            'closure requires all original sources and rounds to be settled')
    boards = []
    workflow = _load_json(_vault_path(domain.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json"))
    boards.append(workflow['kanban']['board_id'])
    for path in (domain.root(run_id) / 'native').rglob('operation.json'):
        operation = _load_json(path)
        require(operation.get('workflow_mode') == 'incremental_v1'
                and operation['incremental']['run_id'] == run_id,
                'native closure inventory escaped the incremental run')
        boards.append(operation['board'])
    boards = sorted(set(boards))
    require(all(isinstance(board, str) and board for board in boards), 'closure is missing a native board')
    kb, kc, _ = native.runtime()
    for board in boards:
        with closing(kc.connect(board=board)) as connection:
            tasks = kb.list_tasks(connection)
            require(not any(t.claim_lock or t.status in ('running', 'ready', 'todo', 'triage') for t in tasks), 'native board remains active')
    # The source watcher observes the last completion independently. Let that
    # ordinary handoff finish, while retaining a bounded, fail-closed drain.
    deadline = time.monotonic() + 30
    while any(workflow_processes(run['source_workflow_id'], board) for board in boards):
        require(time.monotonic() < deadline, 'native consumers still active')
        time.sleep(0.2)
    delivery = None
    for round_id in reversed(run['round_ids']):
        delivery_path = domain.root(run_id) / 'rounds' / round_id / 'delivery.json'
        if delivery_path.exists():
            if _load_json(delivery_path)['state'] == 'committed':
                delivery = pinned(domain.vault, delivery_path.relative_to(domain.vault).as_posix())
                break
    return {'source_workflow_id': run['source_workflow_id'], 'boards': boards}, delivery


def close(domain, run_id):
    from incremental_closure import finish

    run = domain.status(run_id)
    return finish(domain, run, *closure_inputs(domain, run))
