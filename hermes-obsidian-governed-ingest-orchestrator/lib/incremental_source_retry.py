"""Retry exact stopped source-map jobs that have never consumed a model call."""
from contextlib import closing
import os
from pathlib import Path

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, safe_id
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import service, verified_result

KIND = 'mapping-unconsumed'


def require(condition, message, code='SOURCE_RETRY_UNAVAILABLE'):
    if not condition:
        raise ContractError(code, '$', message)


def controller_alive(root):
    path = root / 'controller.json'
    if not path.is_file():
        return False
    owner = _load_json(path)
    try:
        fields = Path('/proc', str(owner['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return False
    return fields[0] != 'Z' and fields[19] == owner.get('start')


def profile_ready():
    from hermes_cli.profiles import profile_exists
    import run_short_evidence_test as native
    return not profile_exists(native.ASSIGNEE)


def precall_proof(domain, workspace, job, row, task, runs, *, expected=None):
    """Attest the old fail-fast lock error before budget intent and SDK entry."""
    import run_short_evidence_test as native

    operation = _load_json(workspace / 'operation.json')
    run_id = operation['incremental']['run_id']
    run_root = domain.root(run_id)
    attempt_path = job.with_name(job.stem + '.attempt-1.json')
    attempt = _load_json(attempt_path)
    grant = attempt.get('grant', {})
    require(attempt.get('ordinal') == 1 and attempt.get('request') == row
            and grant.get('board') == operation['board'] and grant.get('task_id') == task.id,
            'precall marker does not match the exact original request and native task')
    allowed = {job.name, attempt_path.name}
    require({path.name for path in job.parent.glob(job.stem + '.*')} <= allowed,
            'precall recovery found an audit, result, transport or later attempt')
    error = f'LOCK_BUSY at $: Skill kernel lock is held: {run_root / ".runtime-mutation.lock"}; wait limit=0s'
    old = next((item for item in runs if item.id == grant.get('run_id')), None)
    require(old is not None and old.ended_at is not None and old.outcome == 'gave_up'
            and old.error == error and old.worker_pid
            and old.started_at <= attempt.get('started_at', -1) < old.ended_at + 1,
            'precall marker lacks an exact ended native lock failure')
    key = fingerprint({'request': row['id'], 'ordinal': 1})
    run = domain.status(run_id)
    require(not any(item.get('request_fingerprint') == key for item in run['reservations'].values())
            and not (run_root / 'operations' / ('call-' + key + '.json')).exists()
            and not (run_root / 'runtime-requests' / ('call-' + key + '.json')).exists(),
            'precall attempt may have entered budget reservation')
    kb, _, _ = native.runtime()
    board_dir = kb.kanban_home() if operation['board'] == 'default' else kb.board_dir(operation['board'])
    log = board_dir / 'logs' / (task.id + '.log')
    require(log.is_file() and not log.is_symlink() and not board_dir.is_symlink(),
            'precall native worker log is unavailable')
    raw = log.read_bytes()
    if expected is not None:
        size = expected['worker_log']['prefix_bytes']
        require(len(raw) >= size, 'precall native log was truncated')
        raw = raw[:size]
    text = raw.decode('utf-8', errors='replace')
    require(text.rstrip().endswith('hermes_source_units.validation.ContractError: ' + error)
            and all(marker in text for marker in ('in run_job', 'in reserve_call', 'in mutate',
                'reserve_call(root, original, ordinal, grant)',
                "with _exclusive_lock(domain.root(run_id) / '.runtime-mutation.lock')")),
            'native log does not attest the pre-reservation lock failure chain')
    result = {'contract': 'incremental-precall-attempt/v1',
              'attempt': {'ref': attempt_path.relative_to(domain.vault).as_posix(),
                          'sha256': _sha(attempt_path.read_bytes())}, 'grant': grant,
              'ended_run': {key: getattr(old, key) for key in
                            ('id', 'started_at', 'ended_at', 'outcome', 'error', 'worker_pid')},
              'worker_log': {'path': str(log), 'prefix_bytes': len(raw), 'prefix_sha256': _sha(raw)}}
    if expected is not None:
        require(result == expected, 'precall recovery evidence changed', 'STALE_INPUT')
    return result


def retry_start_ordinal(workspace, row):
    """Keep the old unconsumed marker and use ordinal 2 for the first real call."""
    import run_short_evidence_test as native

    job = workspace / 'jobs' / (row['id'] + '.json')
    old = job.with_name(job.stem + '.attempt-1.json')
    if not old.exists():
        return 1
    operation = _load_json(workspace / 'operation.json')
    require(operation.get('workflow_mode') == 'incremental_v1'
            and operation['incremental']['scope_kind'] == 'source'
            and operation['incremental']['stage'] == 'mapping', 'precall adoption requires source mapping')
    run_id = operation['incremental']['run_id']
    domain = service(Path(operation['vault']), run_id)
    run = domain.status(run_id)
    for ordinal in (2, 3):
        key = fingerprint({'request': row['id'], 'ordinal': ordinal})
        require(not any(value.get('request_fingerprint') == key for value in run['reservations'].values())
                and not (domain.root(run_id) / 'operations' / ('call-' + key + '.json')).exists()
                and not (domain.root(run_id) / 'runtime-requests' / ('call-' + key + '.json')).exists(),
                'a later model call has already reserved consumption')
    matches = []
    for path in (domain.root(run_id) / 'source-retries').glob('*.json'):
        saved = _load_json(path)
        if saved.get('state') != 'committed' or saved.get('contract') != 'incremental-source-retry/v1':
            continue
        evidence = saved['evidence']
        request = saved['request']
        require(request.get('run_id') == run_id and request.get('actor') == run['actor']
                and request.get('retry_kind') == KIND
                and request.get('source_path') == evidence['source_path']
                and request.get('source_sha256') == evidence['source_sha256'],
                'precall recovery journal lost its exact operator request')
        if (evidence['board'] != operation['board'] or evidence['operation_sha256'] != _sha((workspace / 'operation.json').read_bytes())
                or evidence['workspace'] != workspace.relative_to(domain.vault).as_posix()):
            continue
        require(evidence['snapshot_id'] == operation['incremental']['scope_id']
                and {'path': evidence['source_path'], 'sha256': evidence['source_sha256']} in run['source_manifest'],
                'precall recovery journal changed its source snapshot')
        for target in evidence['targets']:
            if target['job'] == job.name and target.get('precall_attempt'):
                require(target['job_sha256'] == _sha(job.read_bytes()), 'adopted mapping job changed')
                matches.append(target)
    require(len(matches) == 1, 'unconsumed attempt needs one exact committed precall recovery')
    target = matches[0]
    kb, _, _ = native.runtime()
    with closing(completion_connection(operation['board'])) as connection:
        task = kb.get_task(connection, target['task_id'])
        require(task and task.created_by == native.CONTRACT and task.body == native.task_body(workspace, job),
                'precall native job binding changed')
        runs = kb.list_runs(connection, task.id, include_active=False)
        precall_proof(domain, workspace, job, row, task, runs, expected=target['precall_attempt'])
    return 2


def inventory(domain, run_id, source_path, *, prefix=None):
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    run = domain.status(run_id)
    control = IncrementalControl(domain.vault, run_id).status()
    require(run['state'] == 'active' and control['state'] in ('active', 'paused'), 'run must be active or durably paused')
    require(not controller_alive(domain.root(run_id)), 'incremental controller is still running')
    source = run['sources'].get(source_path)
    require(source and source['state'] == 'prepared', 'only a prepared source with unfinished mapping is retryable')
    snapshot_id = source['snapshot_id']
    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    pin = next((row for row in run['source_manifest'] if row['path'] == source_path), None)
    require(pin and snapshot['original'] == {'ref': source_path, 'sha256': pin['sha256']}, 'current source snapshot identity changed')
    require(not any(scope['scope_kind'] == 'source' and scope['scope_id'] == snapshot_id
                    for scope in control.get('active_scopes', {}).values()), 'target source still has an admitted executor')
    root = domain.root(run_id) / 'native/source' / snapshot_id / 'mapping'
    operation = _load_json(root / 'operation.json')
    scope = operation.get('incremental', {})
    require(operation.get('workflow_mode') == 'incremental_v1' and operation.get('contract') == native.CONTRACT
            and Path(operation['vault']).resolve() == domain.vault.resolve() and operation['actor'] == run['actor']
            and scope.get('run_id') == run_id and scope.get('scope_kind') == 'source'
            and scope.get('scope_id') == snapshot_id and scope.get('stage') == 'mapping'
            and scope.get('snapshots') == [snapshot_id], 'mapping workspace scope changed')
    require(profile_ready(), 'reserved program assignee cannot be a Hermes profile')
    require(not workflow_processes('', operation['board']), 'target mapping board still has native consumers')
    kb, _, _ = native.runtime()
    with closing(completion_connection(operation['board'])) as connection:
        tasks = kb.list_tasks(connection)
        require(not any(task.claim_lock or task.status in ('running', 'todo', 'triage') for task in tasks), 'target board has live claims or unresolved routing')
        targets, completed = [], []
        for job in sorted((root / 'jobs').glob('*.json')):
            if job.name.endswith('.result.json') or '.attempt-' in job.name or '.audit-' in job.name:
                continue
            payload = _load_json(job)
            row = payload.get('request', {})
            require(job.name == row.get('id', '') + '.json' and row.get('phase') in ('short-topics', 'short-map'), 'unexpected mapping job identity')
            if job.with_suffix('.result.json').is_file():
                result, _ = verified_result(root, row)
                require(result['state'] == 'settled', 'a consumed held result requires its own supported recovery')
                completed.append({'job': job.name, 'sha256': _sha(job.read_bytes())})
                continue
            attempts = list(job.parent.glob(job.stem + '.attempt-*.json'))
            require(not list(job.parent.glob(job.stem + '.audit-*.json')),
                    'unknown or consumed model attempt cannot be retried')
            fingerprints = {fingerprint({'request': row['id'], 'ordinal': ordinal}) for ordinal in (1, 2, 3)}
            require(not any(value.get('request_fingerprint') in fingerprints for value in run['reservations'].values()),
                    'reserved model consumption cannot be retried')
            body = native.task_body(root, job)
            matches = [task for task in tasks if task.body == body]
            require(len(matches) == 1, 'exact original native mapping task is missing or ambiguous')
            task = matches[0]
            states = ('blocked', 'ready') if prefix is not None else ('blocked',)
            require(task.status in states and not task.claim_lock and task.assignee == native.ASSIGNEE
                    and task.created_by == native.CONTRACT and task.workspace_kind == 'dir'
                    and Path(task.workspace_path).resolve() == root.resolve(), 'only the exact stopped native task can be unblocked')
            runs = kb.list_runs(connection, task.id, include_active=True)
            require(runs and all(getattr(item, 'ended_at', None) is not None and item.status != 'running'
                                 and item.outcome != 'completed' for item in runs),
                    'original native task still has an active or unknown run')
            require(not any(any(marker in str(getattr(item, 'error', '') or '').lower()
                                for marker in ('permission', 'access_denied', 'access denied'))
                            or item.outcome in ('permission_denied', 'access_denied', 'execution_uncertain')
                            for item in runs), 'permission or uncertain execution failures require diagnosis')
            target = {'task_id': task.id, 'job': job.name, 'job_sha256': _sha(job.read_bytes()),
                      'body_sha256': fingerprint(body),
                      'ended_runs': [{'id': item.id, 'status': item.status,
                                      'ended_at': item.ended_at, 'outcome': item.outcome} for item in runs]}
            if attempts:
                target['precall_attempt'] = precall_proof(
                    domain, root, job, row, task, runs,
                    expected=next((item.get('precall_attempt') for item in prefix['targets']
                                   if item['task_id'] == task.id), None) if prefix else None)
            targets.append(target)
        require(targets, 'no blocked zero-consumption source mapping task is available')
        require(not any(task.status == 'ready' and task.id not in {row['task_id'] for row in targets} for task in tasks),
                'another ready task is outside the exact retry scope')
    value = {'source_path': source_path, 'source_sha256': pin['sha256'], 'snapshot_id': snapshot_id,
             'workspace': root.relative_to(domain.vault).as_posix(), 'operation_sha256': _sha((root / 'operation.json').read_bytes()),
             'board': operation['board'], 'targets': targets, 'completed': completed}
    if prefix is not None:
        require(value == prefix, 'original retry jobs, native runs or completed evidence changed', 'STALE_INPUT')
    return value


def preview(vault, run_id, source_path):
    value = {'allowed': False, 'retry_kind': KIND, 'reason': '', 'source_path': source_path, 'source_sha256': None}
    try:
        domain = service(Path(vault), run_id)
        run = domain.status(run_id)
        row = next((item for item in run['source_manifest'] if item['path'] == source_path), None)
        if row:
            value['source_sha256'] = row['sha256']
        source = run['sources'].get(source_path, {})
        if source.get('state') == 'failed':
            value['reason'] = '来源转换失败；原件未变时不重复转换，请先处理原始失败原因。'
            return value
        data = inventory(domain, run_id, source_path)
        value.update(allowed=True, reason='原生映射任务已停止且尚未调用模型；可复用原任务重试。',
                     source_sha256=data['source_sha256'])
    except (ContractError, ValueError, KeyError, OSError, ImportError) as error:
        value['reason'] = str(error)
    return value


def retry(vault, request):
    require(not any(os.environ.get(name) for name in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_DB')),
            'source retry requires a trusted operator without a worker database override', 'ACCESS_DENIED')
    required = {'run_id', 'actor', 'operation_id', 'control_revision', 'run_revision', 'source_path', 'source_sha256', 'retry_kind'}
    require(set(request) == required and request['retry_kind'] == KIND, 'exact source retry request required', 'INVALID_SCHEMA')
    safe_id(request['run_id']); safe_id(request['operation_id'])
    domain = service(Path(vault), request['run_id'])
    root = domain.root(request['run_id'])
    start_controller = False
    with _exclusive_lock(root / '.controller.lock', timeout=0):
        journal = root / 'source-retries' / (request['operation_id'] + '.json')
        run, control = domain.status(request['run_id']), IncrementalControl(domain.vault, request['run_id']).status()
        require(request['actor'] == run['actor'] == control['actor'], 'source retry actor changed', 'ACCESS_DENIED')
        if journal.is_file():
            saved = _load_json(journal)
            require(saved['request'] == request, 'source retry operation identifies different inputs', 'IDEMPOTENCY_CONFLICT')
            if saved['state'] == 'committed':
                return saved['receipt']
            data = inventory(domain, request['run_id'], request['source_path'], prefix=saved['evidence'])
        else:
            require(type(request['control_revision']) is int and type(request['run_revision']) is int
                    and request['control_revision'] == control['revision'] and request['run_revision'] == run['revision'],
                    'source retry control or run revision changed', 'REVISION_CONFLICT')
            data = inventory(domain, request['run_id'], request['source_path'])
            require(data['source_sha256'] == request['source_sha256'], 'source retry raw hash changed', 'SOURCE_CHANGED')
            selected = {(data['board'], row['task_id']) for row in data['targets']}
            for prior_path in (root / 'source-retries').glob('*.json'):
                prior = _load_json(prior_path)
                require(prior.get('contract') == 'incremental-source-retry/v1', 'unknown prior source retry evidence')
                prior_targets = {(prior['evidence']['board'], row['task_id']) for row in prior['evidence']['targets']}
                require(not selected & prior_targets, 'the one source retry grant for this native task is already reserved')
            saved = {'contract': 'incremental-source-retry/v1', 'request': request, 'state': 'prepared', 'evidence': data}
            _write_atomic(journal, _json_bytes(saved))
        require(data['source_sha256'] == request['source_sha256'], 'source retry raw hash changed', 'SOURCE_CHANGED')
        from ingest_kanban import KanbanCLI
        cli = KanbanCLI()
        for target in data['targets']:
            cli.unblock(data['board'], target['task_id'])
        inventory(domain, request['run_id'], request['source_path'], prefix=data)
        receipt = {'run_id': request['run_id'], 'source_path': request['source_path'], 'retry_kind': KIND,
                   'task_ids': [row['task_id'] for row in data['targets']], 'reused_completed_jobs': len(data['completed']),
                   'semantic_calls_replayed': 0, 'pause_released': False, 'state': 'ready',
                   'controller_requested': control['state'] == 'active'}
        _write_atomic(journal, _json_bytes({**saved, 'state': 'committed', 'receipt': receipt}))
        start_controller = control['state'] == 'active'
    if start_controller:
        from incremental_ingest_controller import spawn
        spawn(domain.vault, request['run_id'])
    return receipt
