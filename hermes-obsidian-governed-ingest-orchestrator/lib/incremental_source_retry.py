"""Retry exact stopped source-map jobs that have never consumed a model call."""
from contextlib import closing
import os
from pathlib import Path
import re

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, pinned, read_pin, safe_id, verify_mapping_isolation
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import service, verified_result

KIND = 'mapping-unconsumed'
SELECTED = 'incremental-source-retry/v2'
RELEASE = 'incremental-map-operator-retry/v1'
BASE_REQUEST = {'run_id', 'actor', 'operation_id', 'control_revision', 'run_revision', 'source_path', 'source_sha256',
                'retry_kind'}


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


def legacy_wal_preflight(domain, source, operation, task, ended):
    """Recognize the old failure before the worker reached any model setup."""
    import run_short_evidence_test as native

    require(ended.outcome == 'gave_up' and (getattr(ended, 'metadata', None) or {}).get('trigger_outcome') == 'worker_exited'
            and ended.error == 'native worker exited before acknowledgement' and ended.worker_pid
            and 0 < ended.started_at <= ended.ended_at, 'legacy run does not prove a pre-call worker exit')
    seal = read_pin(domain.vault, source['seal'])
    source_board = safe_id(seal['native_completion']['board'])
    kb, _, _ = native.runtime()

    def board_directory(board):
        return kb.kanban_home() if board == 'default' else kb.board_dir(board)

    directory = board_directory(operation['board'])
    log = directory / 'logs' / (safe_id(task.id) + '.log')
    require(directory.resolve(strict=True) == directory and log.resolve(strict=True) == log
            and log.is_file(), 'legacy native log is missing or redirected')
    require(ended.started_at <= log.stat().st_mtime <= ended.ended_at + 1,
            'legacy native log timestamp is outside the exact ended attempt')
    raw = log.read_bytes()
    text = raw.decode('utf-8').replace('\r\n', '\n')
    require(text.count('Traceback (most recent call last):') == 1, 'legacy worker log does not identify one traceback')
    board_root = board_directory(source_board)
    database = board_root / 'kanban.db'
    sidecar = board_root / 'kanban.db-wal'
    require(board_root.resolve(strict=True) == board_root and database.resolve(strict=True) == database
            and database.is_file() and not sidecar.is_symlink()
            and os.access(board_root, os.R_OK | os.W_OK | os.X_OK) and os.access(database, os.R_OK | os.W_OK)
            and (not sidecar.exists() or (sidecar.is_file() and os.access(sidecar, os.R_OK | os.W_OK))),
            'native database or WAL currently has an access failure')
    frames = [(Path(path).name, function) for path, function in re.findall(
        r'^  File "([^"]+)", line \d+, in ([a-z_]+)$', text, re.MULTILINE)]
    expected = [('run_short_evidence_test.py', 'worker'), ('run_short_evidence_test.py', 'own'),
                ('run_short_evidence_test.py', 'live'), ('incremental_native.py', 'check_operation'),
                ('incremental_ingest.py', 'validate_source_snapshot'), ('incremental_sources.py', 'verify_native_completion'),
                ('incremental_sources.py', 'completion_connection'), ('kanban_db_connect.py', 'connect'),
                ('hermes_state_repair.py', 'preflight_db_writability')]
    error = f'sqlite3.OperationalError: kanban.db (kanban.db) is not writable: file {sidecar} is read-only for this user.'
    require(frames[-len(expected):] == expected and text.rstrip().splitlines()[-1].startswith(error)
            and not any(function in ('run_job', 'reserve_call') for _, function in frames),
            'legacy traceback does not attest the source-board WAL preflight before request dispatch')
    return {'contract': 'incremental-map-legacy-wal-preflight/v1', 'source_board': source_board,
            'database': str(database), 'worker_log': {'path': str(log), 'sha256': _sha(raw), 'bytes': len(raw)},
            'native_run_id': ended.id, 'source_seal': source['seal']}


def _attempt_pin(item):
    return {key: getattr(item, key, None) for key in ('id', 'status', 'started_at', 'ended_at', 'outcome', 'error', 'worker_pid')}


def _already_approved(root, workspace, board, job, task):
    if (workspace / 'map-retries' / job.name).exists():
        return True
    for path in (root / 'source-retries').glob('*.json'):
        journal = _load_json(path)
        if journal.get('contract') not in (SELECTED, 'incremental-source-retry/v1'):
            continue
        if journal['evidence']['board'] == board and any(
                target.get('task_id') == task.id for target in journal['evidence']['targets']):
            return True
    return False


def _candidate(domain, run, source, workspace, operation, job, row, task, runs, *, released=False):
    from incremental_map_recovery import TRANSIENT_OUTCOME, count_sends, job_ordinals

    run_root = domain.root(operation['incremental']['run_id'])
    require(task.status in (('blocked', 'ready') if released else ('blocked', 'archived')) and not task.claim_lock
            and not getattr(task, 'current_run_id', None), 'native task is not quiescent', 'NATIVE_ACTIVE')
    require(len(runs) == 1 and runs[0].ended_at is not None and runs[0].outcome == 'gave_up'
            and runs[0].status != 'running', 'task has no single ended failed native attempt', 'NATIVE_HISTORY')
    require(not (workspace / 'receipts' / job.name).exists(), 'request already has a model receipt', 'CONSUMPTION_EVIDENCE')
    keys = {item['request_fingerprint'] for item in run['reservations'].values()}
    require(not job_ordinals(run_root, row, keys), 'request has budget or mutation intent', 'CONSUMPTION_EVIDENCE')
    sends = count_sends(operation, task.id)
    require(sends == 0, 'physical sends cannot be proved zero', 'HTTP_UNKNOWN' if sends is None else 'HTTP_SENT')
    artifacts = {path.name for path in job.parent.glob(job.stem + '.*.json')}
    precall = job.stem + '.attempt-1.json'
    require(not artifacts or artifacts == {precall}, 'request has attempt/result or transport evidence', 'CONSUMPTION_EVIDENCE')
    for path in (workspace / 'call-failures').glob('*.json'):
        require(_load_json(path).get('task_id') != task.id, 'request has an SDK failure record', 'CONSUMPTION_EVIDENCE')
    ended = runs[0]
    proof = {'physical_sends': 0, 'ended_runs': [_attempt_pin(ended)], 'body_sha256': fingerprint(task.body)}
    if artifacts:
        proof['precall_attempt'] = precall_proof(domain, workspace, job, row, task, runs)
        code, reason = 'PRE_RESERVATION_LOCK', 'Bound pre-reservation runtime lock failure'
    elif (getattr(ended, 'metadata', None) or {}).get('trigger_outcome') == TRANSIENT_OUTCOME:
        code, reason = 'TYPED_PRECALL_TRANSIENT', 'Typed temporary startup failure before any physical send'
    else:
        proof['legacy_preflight'] = legacy_wal_preflight(domain, source, operation, task, ended)
        code, reason = 'LEGACY_WAL_PRECALL', 'Original worker failed at source-board WAL preflight before any send'
    return {**proof, 'reason_code': code, 'reason': reason}


def recovery_inventory(domain, run_id, source_path, *, request_ids=None):
    """Classify each exact original Map request without moving any native tasks."""
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    run = domain.status(run_id)
    control = IncrementalControl(domain.vault, run_id).status()
    root = domain.root(run_id)
    source = run['sources'].get(source_path, {})
    pin = next((row for row in run['source_manifest'] if row['path'] == source_path), {})
    snapshot_id = source.get('snapshot_id')
    if request_ids is not None:
        require(isinstance(request_ids, list) and request_ids and len(set(map(safe_id, request_ids))) == len(request_ids),
                'selected Map request IDs must be a nonempty unique list', 'INVALID_SCHEMA')
        request_ids = sorted(request_ids)
    value = {'run_id': run_id, 'run_revision': run['revision'], 'run_state': run['state'],
             'control_revision': control['revision'], 'control_state': control['state'],
             'source_path': source_path, 'source_sha256': pin.get('sha256'), 'snapshot_id': snapshot_id,
             'workspace': None, 'operation_sha256': None, 'board': None, 'mode': 'unavailable', 'native_owner': False,
             'source_state': source.get('state'), 'request_ids': request_ids, 'eligible': [], 'completed': [],
             'isolated': [], 'reason': 'No sealed unfinished Map workspace exists for this source'}

    def finish():
        return {**value, 'evidence_fingerprint': fingerprint(value)}

    if not snapshot_id or source.get('state') not in ('prepared', 'mapping_held', 'mapped') or not pin:
        return finish()
    scope_root = root / 'native/source' / safe_id(snapshot_id) / 'mapping'
    operation_path = scope_root / 'operation.json'
    if not operation_path.is_file():
        return finish()
    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    require(snapshot['original'] == {'ref': source_path, 'sha256': pin['sha256']}, 'source snapshot identity changed',
            'SOURCE_CHANGED')
    operation = _load_json(operation_path)
    scope = operation.get('incremental', {})
    require(operation.get('workflow_mode') == 'incremental_v1' and operation.get('contract') == native.CONTRACT
            and Path(operation['vault']).resolve() == domain.vault.resolve() and operation['actor'] == run['actor'] == control['actor']
            and scope.get('run_id') == run_id and scope.get('scope_kind') == 'source'
            and scope.get('scope_id') == snapshot_id and scope.get('stage') == 'mapping'
            and scope.get('snapshots') == [snapshot_id], 'original Map workspace identity changed', 'STALE_INPUT')
    board = safe_id(operation['board'])
    live = (any(row.get('scope_kind') == 'source' and row.get('scope_id') == snapshot_id
                for row in control.get('active_scopes', {}).values()) or bool(workflow_processes('', board)))
    has_hold = source.get('state') == 'mapping_held' and source.get('mapping_hold', {}).get('execution_hold')
    mode, reason = 'unavailable', 'Current run/control/source state has no supported Map recovery'
    if (run['state'] == 'active' and control['state'] in ('active', 'paused', 'pausing')
            and source['state'] == 'prepared' and (control.get('boundary') or {}).get('stage') != 'closure'):
        mode, reason = 'original', 'The original Map card may run only in an admitted mapping scope'
    elif has_hold or (source.get('map_result_id') and any(
            (scope_root / filename).is_file() for filename in ('execution-hold.json', 'partial-execution-hold.json'))):
        mode, reason = 'supplemental', 'The original Map outcome is sealed and requires linked recovery'
    if (scope_root / 'stop.json').exists() and mode == 'original':
        mode, reason = 'unavailable', 'An original native stop marker requires separate supported reconciliation'
    held_tasks = set()
    if mode == 'supplemental':
        if has_hold:
            verify_mapping_isolation(domain.vault, has_hold, scope_root, run_id=run_id,
                                     snapshot_id=snapshot_id, actor=run['actor'])
            pointer = has_hold
        else:
            pointer = domain.read_map(run_id, source['map_result_id']).get('execution_hold')
            require(pointer, 'the sealed Map has no bound execution hold', 'STALE_INPUT')
        report = read_pin(domain.vault, pointer)
        held_tasks = {item['task_id'] for item in report['jobs'] if item['state'] == 'unconfirmed'}
        value['execution_hold'] = pointer
    profile = profile_ready()
    value.update(workspace=scope_root.relative_to(domain.vault).as_posix(), operation_sha256=_sha(operation_path.read_bytes()),
                 board=board, mode=mode, native_owner=live, reason=reason)
    kb, _, _ = native.runtime()
    with closing(completion_connection(board)) as connection:
        tasks = kb.list_tasks(connection, include_archived=True)
        live = live or any(task.claim_lock or task.status in ('running', 'ready', 'todo', 'triage') for task in tasks)
        value['native_owner'] = live
        seen = set()
        for job in sorted((scope_root / 'jobs').glob('*.json')):
            if '.' in job.stem:
                continue
            row = _load_json(job)['request']
            identifier = safe_id(row['id'])
            require(job.stem == identifier and row.get('phase') in ('short-map', 'short-topics'),
                    'original Map job identity changed', 'STALE_INPUT')
            seen.add(identifier)
            body = native.task_body(scope_root, job)
            matches = [task for task in tasks if task.body == body]
            item = {'request_id': identifier, 'phase': row['phase'], 'job': job.name,
                    'job_sha256': _sha(job.read_bytes()), 'task_id': matches[0].id if len(matches) == 1 else None}
            try:
                require(len(matches) == 1 and matches[0].created_by == native.CONTRACT
                        and matches[0].assignee == native.ASSIGNEE and matches[0].workspace_kind == 'dir'
                        and Path(matches[0].workspace_path).resolve() == scope_root, 'native job binding is missing or ambiguous',
                        'NATIVE_BINDING')
                task = matches[0]
                if job.with_suffix('.result.json').exists():
                    result, proof = verified_result(scope_root, row)
                    require(result['state'] == 'settled', 'native result was acknowledged but did not settle',
                            'CONSUMPTION_EVIDENCE')
                    receipt = scope_root / 'receipts' / job.name
                    value['completed'].append({**item, 'state': 'completed', 'reason_code': 'VERIFIED_RECEIPT',
                                               'reason': 'Reuse verified original native receipt', 'proof': proof,
                                               'receipt': pinned(domain.vault, receipt.relative_to(domain.vault).as_posix())})
                    continue
                require(not live, 'source Map board or scope is still active', 'NATIVE_ACTIVE')
                require(profile, 'reserved Map assignee unexpectedly has a Hermes profile', 'NATIVE_ROUTING')
                require(mode != 'unavailable', reason, 'RUN_STATE')
                require(mode != 'supplemental' or task.id in held_tasks,
                        'task is outside the immutable failed-Map scope', 'HOLD_SCOPE')
                require(not _already_approved(root, scope_root, board, job, task), 'this task already has a retry allocation',
                        'RETRY_ALLOCATED')
                info = _candidate(domain, run, source, scope_root, operation, job, row, task,
                                  kb.list_runs(connection, task.id, include_active=True))
                require(mode != 'original' or task.status == 'blocked', 'original native card cannot be unblocked',
                        'NATIVE_STATE')
            except (ValueError, OSError) as error:
                value['isolated'].append({**item, 'state': 'isolated', 'reason_code': getattr(error, 'code', 'UNVERIFIED'),
                                          'reason': str(error)})
            else:
                if request_ids is None or identifier in request_ids:
                    value['eligible'].append({**item, 'state': 'retryable', **info})
        for missing in sorted(set(request_ids or ()) - seen):
            value['isolated'].append({'request_id': missing, 'task_id': None, 'state': 'isolated',
                                      'reason_code': 'REQUEST_NOT_FOUND', 'reason': 'No exact original Map request exists'})
    return finish()


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
        if saved.get('state') != 'committed' or saved.get('contract') not in ('incremental-source-retry/v1', SELECTED):
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
    from incremental_map_recovery import count_sends, job_ordinals
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
            reservations = {value['request_fingerprint'] for value in run['reservations'].values()}
            require(not job_ordinals(domain.root(run_id), row, reservations)
                    and not (root / 'receipts' / job.name).exists(),
                    'reserved model consumption cannot be retried')
            body = native.task_body(root, job)
            matches = [task for task in tasks if task.body == body]
            require(len(matches) == 1, 'exact original native mapping task is missing or ambiguous')
            task = matches[0]
            states = ('blocked', 'ready') if prefix is not None else ('blocked',)
            require(task.status in states and not task.claim_lock and task.assignee == native.ASSIGNEE
                    and task.created_by == native.CONTRACT and task.workspace_kind == 'dir'
                    and Path(task.workspace_path).resolve() == root.resolve(), 'only the exact stopped native task can be unblocked')
            require(count_sends(operation, task.id) == 0, 'original task does not prove zero physical sends')
            require(not (root / 'map-retries' / job.name).exists(), 'original task has a scoped Map retry allocation')
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


def authorize_selected(vault, request):
    required = BASE_REQUEST | {'request_ids', 'evidence_fingerprint'}
    require(set(request) == required and request['retry_kind'] == KIND, 'exact selected Map recovery request required',
            'INVALID_SCHEMA')
    require(not any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT',
                                                    'HERMES_KANBAN_DB')),
            'selected source retry requires a trusted operator', 'ACCESS_DENIED')
    run_id, operation_id = safe_id(request['run_id']), safe_id(request['operation_id'])
    domain = service(Path(vault), run_id)
    root = domain.root(run_id)
    journal = root / 'source-retries' / (operation_id + '.json')
    with _exclusive_lock(root / '.source-retry-admission.lock', timeout=5, marker=False):
        if journal.is_file():
            saved = _load_json(journal)
            require(saved['contract'] == SELECTED and saved['request'] == request and saved['state'] == 'committed',
                    'source retry operation identifies different inputs', 'IDEMPOTENCY_CONFLICT')
            require(domain.status(run_id)['actor'] == request['actor'], 'source retry actor changed', 'ACCESS_DENIED')
            return saved['receipt']
        data = recovery_inventory(domain, run_id, request['source_path'], request_ids=request['request_ids'])
        require(data['mode'] == 'original' and not data['native_owner'], data['reason'])
        require(data['evidence_fingerprint'] == request['evidence_fingerprint']
                and data['run_revision'] == request['run_revision'] and data['control_revision'] == request['control_revision']
                and data['source_sha256'] == request['source_sha256'], 'selected source retry evidence changed', 'STALE_INPUT')
        latest = domain.status(run_id)
        head = IncrementalControl(domain.vault, run_id).status()
        require(latest['actor'] == request['actor'] == head['actor'], 'source retry actor changed', 'ACCESS_DENIED')
        require(latest['revision'] == data['run_revision'] and head['revision'] == data['control_revision']
                and not any(scope.get('scope_kind') == 'source' and scope.get('scope_id') == data['snapshot_id']
                            for scope in head.get('active_scopes', {}).values()),
                'selected Map source acquired new authority while reviewing', 'STALE_INPUT')
        targets = data['eligible']
        require({item['request_id'] for item in targets} == set(request['request_ids']),
                'one or more selected original Map requests are not safe to retry')
        receipt = {'run_id': run_id, 'source_path': request['source_path'], 'retry_kind': KIND,
                   'task_ids': [target['task_id'] for target in targets], 'reused_completed_jobs': len(data['completed']),
                   'semantic_calls_replayed': 0, 'pause_released': False, 'state': 'approved',
                   'controller_requested': data['control_state'] == 'active'}
        _write_atomic(journal, _json_bytes({'contract': SELECTED, 'request': request, 'state': 'committed',
                                           'evidence': {**data, 'targets': targets}, 'receipt': receipt}))
        return receipt


def _release_pointer(domain, journal, target):
    return {'contract': RELEASE, 'task_id': target['task_id'], 'request_id': target['request_id'],
            'approval': pinned(domain.vault, journal.relative_to(domain.vault).as_posix())}


def verify_selected_release(workspace, operation, job, task, runs, intent):
    """Verify an operator-approved job immediately before native claim."""
    import run_short_evidence_test as native

    run_id = operation['incremental']['run_id']
    domain = service(Path(operation['vault']), run_id)
    run = domain.status(run_id)
    saved = read_pin(domain.vault, intent['approval'])
    request, data = saved['request'], saved['evidence']
    journal = domain.root(run_id) / 'source-retries' / (safe_id(request['operation_id']) + '.json')
    require(saved['contract'] == SELECTED and saved['state'] == 'committed' and request['run_id'] == run_id
            and request['actor'] == run['actor'] == operation['actor'] and request['retry_kind'] == KIND
            and data['snapshot_id'] == operation['incremental']['scope_id'] and data['board'] == operation['board']
            and data['workspace'] == workspace.relative_to(domain.vault).as_posix()
            and data['operation_sha256'] == _sha((workspace / 'operation.json').read_bytes()),
            'selected Map approval lost its original scope')
    row = _load_json(job)['request']
    matches = [target for target in data['targets'] if target['request_id'] == row['id']]
    require(len(matches) == 1, 'selected Map approval lacks the exact request')
    target = matches[0]
    require(intent == _release_pointer(domain, journal, target) and target['task_id'] == task.id
            and target['job'] == job.name and target['job_sha256'] == _sha(job.read_bytes())
            and task.body == native.task_body(workspace, job) and task.created_by == native.CONTRACT
            and task.assignee == native.ASSIGNEE and task.workspace_kind == 'dir'
            and Path(task.workspace_path).resolve() == workspace, 'selected Map approval lost its original task')
    source = run['sources'].get(request['source_path'], {})
    require(run['state'] == 'active' and source.get('state') == 'prepared'
            and source.get('snapshot_id') == data['snapshot_id']
            and {'path': request['source_path'], 'sha256': request['source_sha256']} in run['source_manifest'],
            'selected Map source or original run is no longer dispatchable')
    info = _candidate(domain, run, source, workspace, operation, job, row, task, runs, released=True)
    require(all(target.get(key) == value for key, value in info.items()), 'selected Map retry zero-send evidence changed',
            'STALE_INPUT')


def execute_selected_retries(domain, run_id, workspace, operation, control):
    """Dispatch approved original cards under the controller's active mapping authority."""
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save

    directory = domain.root(run_id) / 'source-retries'
    selected = []
    if not directory.exists():
        return True
    with _exclusive_lock(domain.root(run_id) / '.source-retry-admission.lock', timeout=5, marker=False):
        for journal in sorted(directory.glob('*.json')):
            saved = _load_json(journal)
            if saved.get('contract') != SELECTED or saved.get('state') != 'committed':
                continue
            data = saved['evidence']
            if data['workspace'] != workspace.relative_to(domain.vault).as_posix():
                continue
            require(data['board'] == operation['board'], 'selected Map approval board changed')
            selected.extend((journal, target) for target in data['targets'])
        if not selected:
            return True
        if control.status()['state'] != 'active':
            return False
        kb, _, _ = native.runtime()
        rows = []
        with closing(completion_connection(operation['board'])) as connection:
            for journal, target in selected:
                job = workspace / 'jobs' / (safe_id(target['request_id']) + '.json')
                task = kb.get_task(connection, target['task_id'])
                require(task and not task.claim_lock and task.status in ('blocked', 'ready', 'done', 'archived'),
                        'approved Map task has unexpected native ownership')
                runs = kb.list_runs(connection, task.id, include_active=True)
                if len(runs) > 1:
                    require({item['id'] for item in target['ended_runs']} <= {item.id for item in runs},
                            'approved Map task lost its original attempts')
                    continue  # A prior native claim consumed this one approval.
                intent = _release_pointer(domain, journal, target)
                verify_selected_release(workspace, operation, job, task, runs, intent)
                save(workspace / 'map-retries' / job.name, intent)
                rows.append(_load_json(job)['request'])
    if rows:
        native.stage(workspace, operation, rows, operation['pass_capacity'],
                     mapping_retries={row['id'] for row in rows})
    return control.status()['state'] == 'active'


def retry(vault, request):
    from incremental_ingest_controller import resume_run

    result = authorize_selected(vault, request) if 'request_ids' in request else authorize_retry(vault, request)
    domain = service(Path(vault), request['run_id'])
    resume_run(domain, request['run_id'])
    return result


def authorize_retry(vault, request):
    require(not any(os.environ.get(name) for name in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_DB')),
            'source retry requires a trusted operator without a worker database override', 'ACCESS_DENIED')
    require(set(request) == BASE_REQUEST and request['retry_kind'] == KIND,
            'exact source retry request required', 'INVALID_SCHEMA')
    safe_id(request['run_id']); safe_id(request['operation_id'])
    domain = service(Path(vault), request['run_id'])
    root = domain.root(request['run_id'])
    with _exclusive_lock(root / '.controller.lock', timeout=0), _exclusive_lock(
            root / '.source-retry-admission.lock', timeout=5, marker=False):
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
                require(prior.get('contract') in ('incremental-source-retry/v1', SELECTED), 'unknown prior source retry evidence')
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
    return receipt
