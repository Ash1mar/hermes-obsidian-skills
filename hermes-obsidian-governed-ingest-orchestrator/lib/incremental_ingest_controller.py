"""Recoverable native scheduling for explicit fixed-source incremental runs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import pinned, read_pin
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import mutate, service, verified_result
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


def round_work(domain, run_id, round_id):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save
    from hermes_source_units.incremental_pages import (normalize_page_changes, plan_page_changes,
                                                       prepare_page_revision, validate_page_revision)
    from hermes_source_units.knowledge_build import FileKnowledgeBuildService
    from hermes_source_units.page_governance import POLICY
    from hermes_source_units.vault_finalize import FileVaultFinalizeService
    from fixed_semantic_media import image_catalog, select_images

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
    with control.executing(scope, 'coordination', frozen['input_fingerprint']):
        if coordination_request_path.exists():
            row = _load_json(coordination_request_path)
        else:
            require(not plan_path.exists(), 'saved page changes lack their original coordination request')
            spec = plan_page_changes(build_service, frozen, hydrated)
            spec['view']['source_scope'] = domain.status(run_id)['sources']
            spec['view']['prior_held_changes'] = [held for rid in domain.status(run_id)['round_ids'] if rid != round_id
                for path in [domain.root(run_id) / 'rounds' / rid / 'settlement.json'] if path.is_file()
                for held in _load_json(path).get('held_changes', [])]
            row = bind_request(spec)
            if not row['budget']['fits']:
                return settle_held(domain, run_id, round_id, frozen, 'coordination request exceeds pinned budget')
            save(coordination_request_path, row)
        if not plan_path.exists():
            execute_rows(domain, run_id, coordinator, operation, [row])
        changes = verified_page_changes(build_service, frozen, aliases, hydrated, coordinator, row, plan_path)
    if control.checkpoint(scope, 'coordination', {'round': frozen, 'changes_sha256': native.sha(plan_path)})['state'] != 'active':
        return
    writer, writing_operation = initialize_scope(domain, run_id, 'round', round_id, 'writing', frozen['snapshots'])
    written, held = [], [change for change in changes if change['action'] == 'hold']
    with control.executing(scope, 'writing', frozen['input_fingerprint']):
        preparations = []
        for change in changes:
            if change['action'] not in ('create', 'update'):
                continue
            preparation = writer / 'preparations' / (change['change_id'] + '.json')
            try:
                if preparation.exists():
                    prepared = _load_json(preparation)['prepared']
                    row = _load_json(preparation)['request']
                else:
                    prepared = prepare_page_revision(build_service, change, domain.status(run_id)['actor'])
                    task = {'task': 'page', 'materials': prepared['projection']['view']['materials']}
                    adapter = SimpleNamespace(workflow=SimpleNamespace(knowledge=build_service, vault=domain.vault))
                    catalog = image_catalog(adapter, {'tasks': [task]})
                    images = select_images([task], catalog)
                    row = bind_request(prepared, images=images)
                    require(row['budget']['fits'], 'complete incremental page evidence exceeds model budget')
                    save(preparation, {'prepared': prepared, 'request': row})
                preparations.append((change, preparation, prepared, row))
            except (ContractError, ValueError, FileNotFoundError) as error:
                held.append({'change_id': change['change_id'], 'page_id': change['page_id'],
                             'reason': str(error), 'state': 'held'})
        if preparations:
            execute_rows(domain, run_id, writer, writing_operation, [item[3] for item in preparations])
        for change, preparation, prepared, row in preparations:
            try:
                answer = _load_json(writer / 'receipts' / (row['id'] + '.json'))['answer']
                validate_page_revision(prepared, answer)
                written.append({'preparation': pinned(domain.vault, preparation.relative_to(domain.vault).as_posix()),
                                'native_root': writer.relative_to(domain.vault).as_posix()})
            except (ContractError, ValueError, FileNotFoundError) as error:
                held.append({'change_id': change['change_id'], 'page_id': change['page_id'],
                             'reason': str(error), 'state': 'held'})
        save(root / 'writing-disposition.json', {'written': written, 'held': held})
    if control.checkpoint(scope, 'writing', {'writing_disposition_sha256': native.sha(root / 'writing-disposition.json')})['state'] != 'active':
        return
    if not written:
        reason = 'all planned pages reused' if not held else 'all selected page changes held'
        return settle_held(domain, run_id, round_id, frozen, reason, reused=not held)
    formal, formal_operation = initialize_scope(domain, run_id, 'round', round_id, 'formal-build', frozen['snapshots'])
    finalize = FileVaultFinalizeService(domain.vault)
    selected = [_load_json(_vault_path(domain.vault, row['preparation']['ref']))['prepared']['view']['change'] for row in written]
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
        if not job.with_suffix('.result.json').exists():
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
                    'held_changes': held, 'delivery': pinned(domain.vault, (root / 'delivery.json').relative_to(domain.vault).as_posix())})


def settle_held(domain, run_id, round_id, frozen, reason, *, reused=False):
    return mutate(domain, run_id, 'settle_round', 'settled-' + round_id, round_id=round_id,
                  disposition={'round_input_fingerprint': frozen['input_fingerprint'],
                               'state': 'reused' if reused else 'held', 'reason': reason})


def recover_delivery(domain, run_id, round_id, operation_id, *, stop_after=None, disposition='continue',
                     isolation=None, settle_terminal=False):
    import run_short_evidence_test as native

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
    job = root / 'jobs' / (row['id'] + '.json')
    if not job.with_suffix('.result.json').exists():
        require(domain.status(run_id)['active_round_id'] == round_id,
                'delivery recovery must target the current frozen round')
        with control.executing({'scope_kind': 'round', 'scope_id': round_id}, 'formal-build', frozen['input_fingerprint']):
            native.stage(root, operation, [row], 1)
    result, _ = verified_result(root, row)
    require(result['state'] == 'settled', 'delivery recovery did not settle its native request')
    output = result['delivery']
    if settle_terminal and output['state'] in ('committed', 'aborted'):
        held = _load_json(round_root / 'writing-disposition.json')['held']
        held = list(held)
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


def close(domain, run_id):
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    run = domain.status(run_id)
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
    return mutate(domain, run_id, 'close_run', 'close-' + run_id,
                  native_drained={'source_workflow_id': run['source_workflow_id'], 'boards': boards}, final_delivery=delivery)
