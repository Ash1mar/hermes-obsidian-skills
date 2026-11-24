"""Source-parallel, finite-round incremental ingest using native Hermes workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_ingest import CONTRACTS, FileIncrementalIngestService
from hermes_source_units.incremental_qualification import (BACKOFF_SECONDS, LOCK_WAIT_SECONDS, WAIT_CONTRACT,
    QualificationContention, check_qualification_active, qualification_id, qualification_lock)
from hermes_source_units.incremental_sources import pinned, read_pin, safe_id
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import NativeCompletionUnconfirmed, mutate, service, verified_result
from short_evidence_pass import load_sources, require, request as semantic_request, seed_requests, mapping_groups, MAP_CONTRACT

MAPPING_DEFERRED = object()


def qualification_wait_path(root, snapshot_id):
    return root / 'source-qualification' / safe_id(snapshot_id) / 'wait.json'


def qualification_wait(root, snapshot_id):
    path = qualification_wait_path(root, snapshot_id)
    return _load_json(path) if path.is_file() else None


def record_qualification_wait(domain, run_id, snapshot_id, error):
    root = domain.root(run_id)
    previous = qualification_wait(root, snapshot_id)
    attempts = list(previous['attempts']) if previous else []
    require(len(attempts) <= len(BACKOFF_SECONDS), 'source qualification wait allowance already exhausted')
    waiting = len(attempts) < len(BACKOFF_SECONDS)
    now = time.time()
    retry_at = now + BACKOFF_SECONDS[len(attempts)] if waiting else None
    attempts.append({'attempt': len(attempts) + 1, 'lock': error.lock.relative_to(domain.vault).as_posix(),
                     'error_code': error.code, 'wait_ms': error.wait_ms, 'observed_at': now})
    value = {'contract': WAIT_CONTRACT, 'run_id': run_id, 'snapshot_id': snapshot_id,
             'qualification_id': qualification_id(run_id, snapshot_id), 'attempts': attempts,
             'state': 'waiting' if waiting else 'exhausted', 'retry_at': retry_at}
    _write_atomic(qualification_wait_path(root, snapshot_id), _json_bytes(value))


def mark_qualification_succeeded(root, snapshot_id):
    value = qualification_wait(root, snapshot_id)
    if value and value['state'] == 'waiting':
        _write_atomic(qualification_wait_path(root, snapshot_id),
                      _json_bytes({**value, 'state': 'qualified', 'retry_at': None}))


def request_path(domain, run_id):
    return domain.root(run_id) / 'runtime-request.json'


def initialize_scope(domain, run_id, kind, scope_id, stage, snapshots, *, recovery_id=None, continuation=None,
                     amendment=None):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save
    from orchestration import configuration

    directory = domain.root(run_id) / 'native' / kind / scope_id / stage
    if recovery_id:
        directory = directory / 'recoveries' / safe_id(recovery_id)
    path = directory / 'operation.json'
    if path.is_file():
        operation = _load_json(path)
        require(operation['incremental']['snapshots'] == snapshots, 'incremental workspace inputs changed')
        require(operation.get('writing_continuation') == continuation, 'incremental recovery binding changed')
        require(operation.get('writing_amendment') == amendment, 'incremental amendment binding changed')
        return directory, operation
    run = domain.status(run_id)
    cfg = configuration()
    operation = {'contract': native.CONTRACT, 'workflow_mode': 'incremental_v1', 'actor': run['actor'],
                 'vault': str(domain.vault), 'board': 'incremental-' + fingerprint([run_id, kind, scope_id, stage, recovery_id])[:32],
                 'origin': {'workflow_id': run['source_workflow_id'], 'snapshots': snapshots},
                 'incremental': {'run_id': run_id, 'scope_kind': kind, 'scope_id': scope_id, 'stage': stage,
                                 'snapshots': snapshots, 'input_fingerprint': fingerprint([snapshots, scope_id])},
                 'pass_capacity': cfg['pass_worker_concurrency'], 'page_capacity': cfg['reduce_page_concurrency']}
    if kind == 'round' and stage in ('coordination', 'writing') and recovery_id is None:
        operation['network_recovery'] = True
    if continuation is not None:
        require(kind == 'round' and stage == 'writing' and recovery_id is not None,
                'writing continuation requires its own round writing scope')
        operation['writing_continuation'] = continuation
    if amendment is not None:
        require(kind == 'round' and stage == 'writing' and recovery_id is not None and continuation is None,
                'writing amendment requires its own round writing scope')
        operation['writing_amendment'] = amendment
    kb, _, _ = native.runtime()
    if not any(board['slug'] == operation['board'] for board in kb.list_boards()):
        kb.create_board(operation['board'], name='Incremental ' + scope_id, default_workdir=str(directory))
    save(path, operation)
    return directory, operation


def execute_rows(domain, run_id, root, operation, rows):
    import run_short_evidence_test as native
    from incremental_recovery import pending_request, request_files
    from incremental_writing import PHASE as PAGE_FRAGMENT

    if operation.get('network_recovery') and rows:
        from incremental_network import execute

        return execute(domain, run_id, root, operation, rows, settle_usage)

    bounded_coordination = bool(rows) and all(row['phase'] == 'incremental-page-plan' and (
        row['view'].get('planning_revision', 0) >= 4 or row['view'].get('coordination_repair')) for row in rows)
    if bounded_coordination:
        # Existing unacknowledged work stays in its original budget and arena.
        # A saved job without a lease or charge is dispatchable only if its
        # exact task is still ready or has not yet been created.
        kb, kc, _ = native.runtime()
        with closing(kc.connect(board=operation['board'])) as connection:
            tasks = {}
            for task in kb.list_tasks(connection):
                require(task.body not in tasks and task.created_by == operation['contract'],
                        'coordination board has a duplicate or unbound task')
                require(not task.claim_lock and task.status != 'running', 'coordination board still has an active task')
                tasks[task.body] = task
        reservations = {item['request_fingerprint'] for item in domain.status(run_id)['reservations'].values()}
        waiting = []
        for row in rows:
            job, attempts = request_files(root, row)
            if job.with_suffix('.result.json').is_file():
                continue
            require(not (root / 'stop.json').exists(), 'stopped coordination requires its supported recovery')
            charged = any(fingerprint({'request': row['id'], 'ordinal': n}) in reservations for n in (1, 2))
            activity = any(job.parent.glob(job.stem + '.program-retry-*.json')) or any(
                job.parent.glob(job.stem + '.audit-*.json'))
            if job.is_file():
                require(_load_json(job)['request'] == row, 'saved coordination job differs from its bound request')
                task = tasks.get(native.task_body(root, job))
                if task is not None and task.status != 'ready':
                    continue
            if not attempts and not charged and not activity:
                waiting.append(row)
    else:
        waiting = [row for row in rows if pending_request(root, operation, row, allow_precall=True)]
    if waiting:
        native.stage(root, operation, waiting, operation['page_capacity']
                     if waiting[0]['phase'] in ('incremental-page-write', PAGE_FRAGMENT) else operation['pass_capacity'])
    results = []
    for row in rows:
        if bounded_coordination and not (root / 'jobs' / (row['id'] + '.result.json')).is_file():
            continue
        try:
            result, proof = verified_result(root, row)
        except NativeCompletionUnconfirmed:
            if bounded_coordination:
                continue
            raise
        results.append((row, result, proof))
        settle_usage(domain, run_id, root, row, result)
    return results


def settle_usage(domain, run_id, root, row, result):
    from hermes_state import SessionDB
    from workflow_metrics import usage_for

    database = SessionDB(read_only=True)
    path = database.db_path
    database.close()
    for attempt in result['attempts']:
        key = fingerprint({'request': row['id'], 'ordinal': attempt['ordinal']})
        budget_id, reservation = next((k, v) for k, v in domain.status(run_id)['reservations'].items()
                                      if v['request_fingerprint'] == key)
        if reservation['state'] == 'settled':
            continue
        usage = usage_for([attempt], path)
        if not attempt.get('model') or usage['unknown_samples']['input'] or usage['unknown_samples']['output']:
            continue
        actual = {'calls': 1, 'input_tokens': int(usage['known_totals']['input']),
                  'output_tokens': int(usage['known_totals']['output'])}
        record = {'request_fingerprint': key, 'actual': actual, 'native_usage': usage}
        ref = root / 'usage' / (key + '.json')
        _write_atomic(ref, _json_bytes(record))
        mutate(domain, run_id, 'settle_budget', 'usage-' + key[:64], budget_id=budget_id, actual=actual,
               usage=pinned(domain.vault, ref.relative_to(domain.vault).as_posix()))


def source_pin(snapshot):
    require(isinstance(snapshot.get('bundle'), dict),
            'incremental short evidence mapping requires a verified governed Bundle')
    return {'original': snapshot['original']['ref'], 'resource_id': snapshot['identity']['resource_id'],
            'unit_set_id': snapshot['unit_set']['id'], 'bundle_manifest': snapshot['bundle']['manifest_ref']}


def source_alias(run, snapshot):
    return 'S' + str(next(i for i, row in enumerate(run['source_manifest'], 1)
                         if row['path'] == snapshot['original']['ref']))


def map_source(vault, run_id, snapshot_id):
    import run_short_evidence_test as native
    from incremental_map_recovery import (MapAdmissionDeferred, execute_map_rows, inspect_map_rows,
                                          required_hold_reason, source_execution_hold)
    from prepare_short_evidence_test import advance, save
    from short_page_contract import CONTRACT as PAGE_CONTRACT

    domain = service(Path(vault), run_id)
    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    control = IncrementalControl(domain.vault, run_id)
    scope = {'scope_kind': 'source', 'scope_id': snapshot_id}
    with ExitStack() as executor:
        try:
            executor.enter_context(control.executing(scope, 'mapping', fingerprint(snapshot)))
            qualify(domain, run_id, snapshot_id)
            mark_qualification_succeeded(domain.root(run_id), snapshot_id)
            check_qualification_active(control)
        except QualificationContention as error:
            record_qualification_wait(domain, run_id, snapshot_id, error)
            return MAPPING_DEFERRED
        except ContractError as error:
            if error.code != 'INCREMENTAL_PAUSED':
                raise
            return MAPPING_DEFERRED
        root, operation = initialize_scope(domain, run_id, 'source', snapshot_id, 'mapping', [snapshot_id])
        alias = source_alias(domain.status(run_id), snapshot)
        sources = load_sources(domain.vault, [source_pin(snapshot)], aliases={snapshot['identity']['resource_id']: alias},
                               actor=domain.status(run_id)['actor'])
        for source in sources.values():
            source['incremental_snapshot_id'] = snapshot_id
        seeds, groups = seed_requests(sources), mapping_groups(sources, MAP_CONTRACT)
        recorded = []
        for path in sorted((root / 'jobs').glob('*.json')):
            if len(path.stem) != 64 or any(character not in '0123456789abcdef' for character in path.stem):
                continue
            row = _load_json(path)['request']
            require(path.stem == row['id'] and row['phase'] in ('short-topics', 'short-map'),
                    'unexpected job in source mapping scope')
            recorded.append(row)
        existing, holds, _, _ = inspect_map_rows(root, operation, recorded)
        acknowledged = {row['id']: (row, result, proof) for row, result, proof in existing}
        for row, result, _ in existing:
            settle_usage(domain, run_id, root, row, result)
        dispatch_started = False
        while True:
            reason = required_hold_reason(root, holds)
            if reason:
                evidence = source_execution_hold(domain, run_id, root, operation, holds, reason)
                return mutate(domain, run_id, 'hold_source_mapping', 'map-execution-held-' + snapshot_id[-48:],
                              source_path=snapshot['original']['ref'], snapshot_id=snapshot_id, execution_hold=evidence)
            verified_receipts = {key for key, (_, result, _) in acknowledged.items() if result['state'] == 'settled'}
            plan = advance(sources, seeds, groups, root / 'receipts', native.LIMIT,
                           native_holds=holds, map_receipt_ids=verified_receipts, mapping_contract=MAP_CONTRACT,
                           page_contract=PAGE_CONTRACT)
            if plan['stage'] not in ('topic-seed', 'map', 'local-map'):
                require('reduced' in plan, 'source Map stopped without a coverage result')
                break
            if not dispatch_started and control.status()['state'] != 'active':
                return MAPPING_DEFERRED
            dispatch_started = True
            try:
                results, isolated = execute_map_rows(domain, run_id, root, operation, plan['requests'], settle_usage)
            except MapAdmissionDeferred:
                return MAPPING_DEFERRED
            acknowledged.update({row['id']: (row, result, proof) for row, result, proof in results})
            holds.update(isolated)
        verified = [(row, proof) for row, result, proof in acknowledged.values() if result['state'] == 'settled']
        if not any(row['phase'] == 'short-map' for row, _ in verified):
            evidence = source_execution_hold(domain, run_id, root, operation, holds, 'no-verified-results')
            return mutate(domain, run_id, 'hold_source_mapping', 'map-execution-held-' + snapshot_id[-48:],
                          source_path=snapshot['original']['ref'], snapshot_id=snapshot_id, execution_hold=evidence)
        proofs = []
        receipts = []
        for row, proof in sorted(verified, key=lambda item: item[0]['id']):
            path = root / 'jobs' / (row['id'] + '.json')
            proofs.append({'proof': proof, 'task_body': json.loads(native.task_body(root, path))})
            receipt = root / 'receipts' / path.name
            receipts.append(pinned(domain.vault, receipt.relative_to(domain.vault).as_posix()))
        reduced_path = root / 'mapped.json'
        save(reduced_path, plan['reduced'])
        execution_hold = ({'execution_hold': source_execution_hold(domain, run_id, root, operation, holds, 'partial')}
                          if holds else {})
        aliases = {key: {'unit_ref': item['source_ref']['unit_ref'], 'sha256': item['sha256'],
                         'snapshot_id': snapshot_id} for source in sources.values() for key, item in source['items'].items()}
        coverage = [{'unit_ref': item['source_ref']['unit_ref'],
                     'disposition': {'linked': 'mapped', 'excluded': 'excluded'}.get(
                         plan['reduced']['dispositions'][key]['status'], 'held'),
                     'reason': plan['reduced']['dispositions'][key].get('reason', 'source Map coverage disposition')}
                    for source in sources.values() for key, item in source['items'].items()]
        summary = fingerprint({'snapshot': snapshot_id, 'reduced': native.sha(reduced_path)})
        budget = mutate(domain, run_id, 'reserve_budget', 'map-budget-' + summary[:48], reservation={
            'request_fingerprint': summary, 'phase': 'mapping', 'max_calls': 0, 'max_input_tokens': 0, 'max_output_tokens': 0})
        usage_path = root / 'usage' / 'map-summary.json'
        _write_atomic(usage_path, _json_bytes({'request_fingerprint': summary,
                                             'actual': {'calls': 0, 'input_tokens': 0, 'output_tokens': 0}}))
        mutate(domain, run_id, 'settle_budget', 'map-summary-' + summary[:48], budget_id=budget['budget_id'],
               actual={'calls': 0, 'input_tokens': 0, 'output_tokens': 0}, usage=pinned(domain.vault, usage_path.relative_to(domain.vault).as_posix()))
        return mutate(domain, run_id, 'commit_source_map', 'map-' + snapshot_id[-56:], snapshot_id=snapshot_id,
            budget_id=budget['budget_id'], request_fingerprint=summary, native_completion=proofs[-1]['proof'],
            task_body=proofs[-1]['task_body'], native_completions=proofs, alias_table=aliases, coverage=coverage,
            results=[pinned(domain.vault, reduced_path.relative_to(domain.vault).as_posix()), *receipts],
            unresolved=plan['reduced'].get('unresolved', []), **execution_hold)


def qualify(domain, run_id, snapshot_id):
    """Use the deterministic source binding rules under the incremental seal."""
    scripts = Path(__file__).resolve().parents[2] / 'hermes-obsidian-controlled-ingest/scripts'
    sys.path.insert(0, str(scripts))
    from governance_repository import GovernanceError, JsonGovernanceRepository
    from source_governance_resolution import digest
    from prepare_short_evidence_test import save

    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    run = domain.status(run_id)
    control = IncrementalControl(domain.vault, run_id)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    repo = JsonGovernanceRepository(domain.vault)
    wid = run['source_workflow_id']
    operation_id = qualification_id(run_id, snapshot_id)
    transaction = domain.vault / '_system/metadata/source-governance-resolutions' / operation_id / 'transaction.json'
    root = domain.root(run_id) / 'source-qualification' / snapshot_id
    # Serialize qualifiers across runs. A source worker can still register its
    # own result between preview and apply; retry one uncommitted revision race.
    with qualification_lock(_vault_path(domain.vault, '_system/metadata/.incremental-source-qualification.lock'),
                            time.monotonic(), lambda: check_qualification_active(control)):
        if transaction.is_file():
            resolution = _load_json(transaction)['request']
            require(resolution['incremental_run_id'] == run_id and resolution['snapshot_id'] == snapshot_id,
                    'saved qualification belongs to another incremental source')
            result = repo.resolve_sources(resolution, qualification_deadline=deadline)
            save(root / 'receipt.json', {key: value for key, value in result.items() if key != 'replayed'})
            return result
        for attempt in (1, 2):
            organization, registry = repo.load_state()
            workflow = _load_json(_vault_path(domain.vault, f'_system/ledgers/ingest-workflows/{wid}.json'))
            resolution = {'workflow_id': wid, 'workflow_ref': f'_system/ledgers/ingest-workflows/{wid}.json',
                          'actor': run['actor'], 'operation_id': operation_id,
                          'reason': 'Explicit incremental source qualification using verified native seal and declarations.',
                          'source_boundary': 'incremental-source/v1', 'incremental_run_id': run_id, 'snapshot_id': snapshot_id,
                          'automatic_approval_activation': True, 'expected_workflow_revision': workflow['revision'],
                          'expected_organization_revision': organization['registry_revision'],
                          'expected_registry_revision': registry['registry_revision'],
                          'resource_ids': [snapshot['identity']['resource_id']]}
            resolution['input_digest'] = digest(resolution)
            try:
                preview = repo.resolve_sources(resolution, dry_run=True, qualification_deadline=deadline)
                require(preview['resolved'], 'source identity has no deterministic qualification')
                resolution['preview_digest'] = preview['preview_digest']
                resolution['input_digest'] = digest({k: v for k, v in resolution.items() if k != 'input_digest'})
                save(root / ('request-' + fingerprint(resolution) + '.json'), resolution)
                result = repo.resolve_sources(resolution, qualification_deadline=deadline)
                save(root / 'receipt.json', result)
                return result
            except GovernanceError as error:
                if attempt != 1 or transaction.exists() or 'Governance revision conflict' not in str(error):
                    raise
        raise AssertionError('bounded qualification attempts exhausted')


def start(request, record, bootstrap_skill):
    from start_vault_workflow import start as provision
    from incremental_ingest_controller import spawn

    require(request.get('workflow_mode') == 'incremental_v1' and not request.get('test_mode'),
            'incremental product mode must be explicit and cannot use historical test import')
    require('incremental_budget' in request and 'incremental_policy' in request,
            'incremental start requires explicit cumulative budgets and fixed round bounds')
    run_id = safe_id(request.get('run_id', request['operation_id']))
    vault = Path(request['vault_path']).resolve()
    existing = vault / '_system/ledgers/incremental-ingest' / run_id / 'runtime-request.json'
    if existing.is_file():
        previous = _load_json(existing)
        require(previous['source_request'] == {**request, 'incremental_run_id': run_id}, 'incremental start inputs changed')
        return service(vault, run_id).status(run_id)
    source_request = {**request, 'incremental_run_id': run_id}
    provision(source_request, record, bootstrap_skill, prepare_only=True, incremental_bootstrap=True)
    require(type(request.get('incremental_manual_sources', False)) is bool,
            'manual source admission must be an explicit boolean')
    manifest = [{'path': request.get('incremental_reuse_originals', {}).get(str(Path(p).resolve()),
                       f"10_Raw/Incremental/{request['operation_id']}/" + Path(p).name),
                 'sha256': _sha(Path(p).resolve(strict=True).read_bytes())}
                for p in request['sources']]
    control = IncrementalControl(vault, run_id)
    control.initialize(request['actor'], request.get('incremental_pause_after', []),
                       manual_sources=manifest if request.get('incremental_manual_sources') else None)
    domain = service(vault, run_id)
    initial = domain.start({'workflow_mode': 'incremental_v1', 'run_id': run_id,
        'operation_id': request['operation_id'], 'actor': request['actor'], 'sources': manifest,
        'source_workflow_id': 'ingest-' + request['operation_id'], 'contracts': CONTRACTS,
        'policy': request['incremental_policy'], 'budget': request['incremental_budget'],
        'control_ref': control.path.relative_to(vault).as_posix()})
    from prepare_short_evidence_test import save
    save(request_path(domain, run_id), {'source_request': source_request, 'record': str(Path(record).resolve()),
                                      'bootstrap_skill': str(Path(bootstrap_skill).resolve()),
                                      'provider_config': request['incremental_provider_config']})
    if control.checkpoint({'scope_kind': 'run', 'scope_id': run_id}, 'prepare', manifest)['state'] != 'active':
        return initial
    provision(source_request, record, bootstrap_skill, incremental_bootstrap=True)
    spawn(vault, run_id)
    return domain.status(run_id)
