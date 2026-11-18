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
            require(not list(job.parent.glob(job.stem + '.attempt-*.json'))
                    and not list(job.parent.glob(job.stem + '.audit-*.json')),
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
            targets.append({'task_id': task.id, 'job': job.name, 'job_sha256': _sha(job.read_bytes()),
                            'body_sha256': fingerprint(body),
                            'ended_runs': [{'id': item.id, 'status': item.status,
                                            'ended_at': item.ended_at, 'outcome': item.outcome} for item in runs]})
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
