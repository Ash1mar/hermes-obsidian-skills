"""Bound task-level recovery for one original incremental source Map."""
from contextlib import closing
from pathlib import Path
import sqlite3

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, pinned
from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import NativeCompletionUnconfirmed, verified_result
from short_evidence_pass import require

TRANSIENT_OUTCOME = 'map_startup_transient'
RETRY = 'incremental-map-precall-retry/v1'
ISOLATION = 'incremental-map-execution-isolation/v1'
JOB_HOLD = 'incremental-map-job-hold/v1'


class MapAdmissionDeferred(ValueError):
    """The current control latch forbids a new automatic attempt."""


def is_map(operation):
    scope = operation.get('incremental') or {}
    return (operation.get('workflow_mode') == 'incremental_v1' and scope.get('scope_kind') == 'source'
            and scope.get('stage') == 'mapping')


def transient_error(root, operation, error):
    if not is_map(operation) or not isinstance(error, ContractError):
        return False
    if error.code == 'NATIVE_BOARD_BUSY':
        return True
    lock = root.parents[3] / '.runtime-mutation.lock'
    return (error.code in ('LOCK_BUSY', 'LOCK_TIMEOUT')
            and error.message.startswith(f'Skill kernel lock is held: {lock}; wait limit='))


def active_admission(operation):
    return IncrementalControl(Path(operation['vault']), operation['incremental']['run_id']).status()['state'] == 'active'


def job_pin(operation, path):
    vault = Path(operation['vault'])
    return pinned(vault, path.relative_to(vault).as_posix())


def ordinal_reservations(run_root):
    return {row['request_fingerprint'] for row in _load_json(run_root / 'run.json')['reservations'].values()}


def job_ordinals(run_root, row, reservations):
    keys = {fingerprint({'request': row['id'], 'ordinal': n}) for n in (1, 2, 3)}
    return [key for key in sorted(keys) if key in reservations
            or any((run_root / directory / ('call-' + key + '.json')).exists()
                   for directory in ('operations', 'runtime-requests'))]


def count_sends(operation, task_id):
    from model_transport import journal_path

    database = journal_path()
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
            count, = connection.execute("SELECT count(*) FROM sends WHERE json_extract(evidence,'$.binding.board')=? "
                                        "AND json_extract(evidence,'$.binding.task_id')=?",
                                        (operation['board'], task_id)).fetchone()
        return count
    except (sqlite3.Error, OSError):
        return None  # Missing audit access proves neither a send nor its absence.


def retry_evidence(root, operation, job, row, task, runs, *, released=False):
    if (task.status != ('ready' if released else 'blocked') or task.claim_lock or len(runs) != 1
            or runs[0].outcome != 'gave_up' or runs[0].ended_at is None
            or (runs[0].metadata or {}).get('trigger_outcome') != TRANSIENT_OUTCOME):
        return None
    native = runs[0]
    if (job_ordinals(root.parents[3], row, ordinal_reservations(root.parents[3]))
            or any(job.parent.glob(job.stem + '.*.json'))
            or (root / 'receipts' / job.name).exists() or count_sends(operation, task.id) != 0):
        return None
    return {'contract': RETRY, 'run_id': operation['incremental']['run_id'],
            'snapshot_id': operation['incremental']['scope_id'], 'board': operation['board'],
            'job': job_pin(operation, job), 'operation': job_pin(operation, root / 'operation.json'),
            'task_id': task.id, 'native_run_id': native.id, 'native_ended_at': native.ended_at,
            'native_error': native.error, 'physical_sends': 0}


def verify_retry(root, operation, job, task, runs):
    from incremental_source_retry import RELEASE, verify_selected_release

    intent = _load_json(root / 'map-retries' / job.name)
    if intent.get('contract') == RELEASE:
        verify_selected_release(root, operation, job, task, runs, intent)
        return
    row = _load_json(job)['request']
    require(retry_evidence(root, operation, job, row, task, runs, released=task.status == 'ready') == intent,
            'automatic Map retry no longer has its exact zero-send evidence')


def save_hold(root, operation, job, task, native, *, result=None):
    from prepare_short_evidence_test import save

    state = 'held' if result is not None else 'unconfirmed'
    reason = 'native request held' if result is not None else 'native Map completion is unconfirmed'
    record = {'contract': JOB_HOLD, 'state': state, 'reason': reason,
              'job': job_pin(operation, job), 'task_id': task.id, 'run_id': native.id,
              'native_outcome': native.outcome, 'ended_at': native.ended_at,
              'native_error': native.error}
    observed_result = job.with_suffix('.result.json')
    if observed_result.is_file():
        observed = _load_json(observed_result)
        require(observed['request_id'] == job.stem and observed['task_id'] == task.id and observed['run_id'] == native.id,
                'unacknowledged Map result does not bind its exact native job')
        record['observed_result'] = job_pin(operation, observed_result)
    receipt = root / 'receipts' / job.name
    if receipt.is_file():
        record['observed_receipt'] = job_pin(operation, receipt)
    path = root / 'mapping-dispositions' / job.name
    if path.is_file() and _load_json(path)['run_id'] != native.id:
        path = path.with_name(f'{job.stem}-attempt-{native.id}.json')
    save(path, record)
    return {'request_id': job.stem, 'reason': reason, 'evidence': job_pin(operation, path),
            'job': record['job'], 'task_id': task.id, 'run_id': native.id, 'state': state,
            **({'result': record['observed_result']} if 'observed_result' in record else {})}


def inspect_map_rows(root, operation, rows):
    """Use native acknowledgements and quarantine each incomplete original job."""
    import run_short_evidence_test as native
    from incremental_recovery import pending_request, request_files

    require(is_map(operation), 'expected the original incremental source Map scope')
    kb, _, _ = native.runtime()
    results, holds, waiting, retrying = [], {}, [], {}
    reservations = ordinal_reservations(root.parents[3]) if rows else set()
    with closing(completion_connection(operation['board'])) as connection:
        tasks = {}
        for task in kb.list_tasks(connection, include_archived=True):
            require(task.body not in tasks and task.created_by == operation['contract'],
                    'Map board has a duplicate or unbound native task')
            require(not task.claim_lock and task.status != 'running', 'Map board has an active native task')
            tasks[task.body] = task
        for row in rows:
            job, attempts = request_files(root, row)
            if not job.is_file():
                require(not attempts and not any(job.parent.glob(job.stem + '.*.json'))
                        and not (root / 'receipts' / job.name).exists()
                        and not job_ordinals(root.parents[3], row, reservations), 'Map request has evidence without its original job')
                waiting.append(row)
                continue
            require(_load_json(job)['request'] == row, 'saved Map request changed')
            task = tasks.get(native.task_body(root, job))
            result_path = job.with_suffix('.result.json')
            if result_path.is_file():
                try:
                    result, proof = verified_result(root, row)
                except NativeCompletionUnconfirmed:
                    pass
                else:
                    results.append((row, result, proof))
                    if result['state'] == 'held':
                        require(task and task.id == result['task_id'], 'held Map result lost its exact native task')
                        ended = next(r for r in kb.list_runs(connection, task.id, include_active=False)
                                     if r.id == result['run_id'])
                        holds[row['id']] = save_hold(root, operation, job, task, ended, result=result)
                    continue
            if task is None:
                require(not attempts and not any(job.parent.glob(job.stem + '.*.json'))
                        and not (root / 'receipts' / job.name).exists()
                        and not job_ordinals(root.parents[3], row, reservations), 'saved Map job left unbound attempt evidence')
                waiting.append(row)
                continue
            runs = kb.list_runs(connection, task.id, include_active=False)
            require(all(item.ended_at is not None and item.outcome != 'completed' for item in runs),
                    'acknowledged Map request lost its immutable completion result')
            if task.status == 'ready':
                intent = root / 'map-retries' / job.name
                if intent.is_file():
                    verify_retry(root, operation, job, task, runs)
                    retrying[row['id']] = _load_json(intent)
                    continue
                require(not result_path.exists() and not (root / 'receipts' / job.name).exists()
                        and not job_ordinals(root.parents[3], row, reservations), 'ready Map request may have consumed a call')
                if pending_request(root, operation, row, allow_precall=True):
                    waiting.append(row)
                continue
            require(task.status in ('blocked', 'archived') and runs,
                    'stopped Map job lacks an exact native failure history')
            retry = retry_evidence(root, operation, job, row, task, runs)
            if retry:
                retrying[row['id']] = retry
                continue
            holds[row['id']] = save_hold(root, operation, job, task, max(runs, key=lambda run: run.id))
    return results, holds, waiting, retrying


def execute_map_rows(domain, run_id, root, operation, rows, settle_usage):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save

    results, holds, waiting, retrying = inspect_map_rows(root, operation, rows)
    if waiting:
        require(not (root / 'stop.json').exists(), 'stopped source Map requires its supported recovery')
        native.stage(root, operation, waiting, operation['pass_capacity'])
        results, holds, waiting, retrying = inspect_map_rows(root, operation, rows)
    if retrying:
        if not active_admission(operation):
            raise MapAdmissionDeferred('automatic Map retry waits for an active control scope')
        for key, record in retrying.items():
            save(root / 'map-retries' / (key + '.json'), record)
        selected = [row for row in rows if row['id'] in retrying]
        require(not (root / 'stop.json').exists(), 'stopped source Map requires its supported recovery')
        native.stage(root, operation, selected, operation['pass_capacity'], mapping_retries=set(retrying))
        results, holds, waiting, retrying = inspect_map_rows(root, operation, rows)
        if retrying and not active_admission(operation):
            raise MapAdmissionDeferred('automatic Map retry waits for an active control scope')
    require(not waiting and not retrying, 'Map dispatch left pending jobs without an accepted disposition')
    for row, result, _ in results:
        settle_usage(domain, run_id, root, row, result)
    return results, holds


def source_execution_hold(domain, run_id, root, operation, holds, reason, *, previous=None):
    from prepare_short_evidence_test import save

    jobs = [{key: row[key] for key in ('job', 'task_id', 'run_id', 'state', 'result', 'evidence') if key in row}
            for _, row in sorted(holds.items())]
    require(jobs, 'source mapping hold requires exact negative native evidence')
    has_primary_gap = reason == 'partial' and required_hold_reason(root, holds) == 'required-map-incomplete'
    path = root / ('partial-execution-hold.json' if has_primary_gap else 'execution-hold.json')
    report = {'contract': ISOLATION, 'run_id': run_id, 'snapshot_id': operation['incremental']['scope_id'],
              'board': operation['board'], 'reason': reason, 'jobs': jobs}
    if previous is not None:
        require(has_primary_gap, 'historical Map hold requires a bound partial primary Map')
        report['historical_hold'] = previous
    save(path, report)
    return pinned(domain.vault, path.relative_to(domain.vault).as_posix())


def required_hold_reason(root, holds, *, allow_acknowledged=False):
    rows = [(_load_json(root / 'jobs' / (request_id + '.json'))['request'], hold)
            for request_id, hold in holds.items()]
    if any(row['phase'] == 'short-topics' for row, _ in rows):
        return 'seed-incomplete'
    if any(row['phase'] == 'short-map' and not row['view'].get('issue')
           and (not allow_acknowledged or hold['state'] != 'held') for row, hold in rows):
        return 'required-map-incomplete'
    return None
