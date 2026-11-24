"""Shared native request history and recovery admission for incremental stages."""
from contextlib import closing

from hermes_source_units.incremental_sources import completion_connection, pinned
from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import verified_result
from short_evidence_pass import require


def request_files(root, row):
    job = root / 'jobs' / (row['id'] + '.json')
    return job, sorted(job.parent.glob(job.stem + '.attempt-*.json'))


def pending_request(root, operation, row, *, allow_precall=False):
    """Preserve completed requests and reject uncertain consumption before dispatch."""
    job, attempts = request_files(root, row)
    if job.with_suffix('.result.json').is_file():
        verified_result(root, row)
        return False
    if attempts:
        require(allow_precall and operation['incremental']['stage'] == 'mapping',
                'attempted native request needs evidence-bound recovery for its stage')
        from incremental_source_retry import retry_start_ordinal

        require(attempts == [job.with_name(job.stem + '.attempt-1.json')]
                and retry_start_ordinal(root, row) == 2,
                'consumed native request has unknown completion; supported recovery is required')
    require(not (root / 'stop.json').exists(), 'stopped native workspace needs evidence-bound recovery for its stage')
    return True


def stopped_history(vault, root, operation, rows, reservations):
    """Inventory native acknowledgements, ended attempts and provably unattempted jobs."""
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    board = operation['board']
    require(not workflow_processes('', board), 'native recovery board still has a worker')
    kb, _, _ = native.runtime()
    budget = {item['request_fingerprint']: (key, item) for key, item in reservations.items()}
    expected_jobs = {root / 'jobs' / (row['id'] + '.json') for row in rows}
    require(len(expected_jobs) == len(rows), 'stopped recovery repeats a native job')
    with closing(completion_connection(board)) as connection:
        tasks = kb.list_tasks(connection)
        require(not any(t.claim_lock or t.status in ('running', 'ready', 'todo', 'triage') for t in tasks),
                'stopped recovery requires a drained native board')
        bodies = {native.task_body(root, job): job for job in expected_jobs if job.is_file()}
        by_job = {}
        for task in tasks:
            job = bodies.get(task.body)
            require(job and job not in by_job and task.created_by == operation['contract'],
                    'stopped native board contains an unbound or duplicate task')
            by_job[job] = task
        history = {}
        for row in rows:
            job, attempts = request_files(root, row)
            task = by_job.get(job)
            if job.is_file():
                require(_load_json(job)['request'] == row, 'stopped request changed')
            runs = {r.id: r for r in kb.list_runs(connection, task.id, include_active=False)} if task else {}
            outcomes = []
            ordinals = {int(path.stem.rsplit('-', 1)[1]) for path in attempts}
            ordinals |= {n for n in (1, 2) if fingerprint({'request': row['id'], 'ordinal': n}) in budget}
            require(all(int(path.stem.rsplit('-', 1)[1]) in ordinals
                        for path in job.parent.glob(job.stem + '.audit-*.json')),
                    'stopped native call audit has no reserved attempt')
            for ordinal in sorted(ordinals):
                key = fingerprint({'request': row['id'], 'ordinal': ordinal})
                require(key in budget, 'stopped attempt has no original budget reservation')
                budget_id, reserved = budget[key]
                require(reserved['phase'] == operation['incremental']['stage'],
                        'stopped attempt reservation belongs to another stage')
                attempt_path = job.with_name(job.stem + f'.attempt-{ordinal}.json')
                audit_path = job.with_name(job.stem + f'.audit-{ordinal}.json')
                outcome = {'ordinal': ordinal, 'budget_id': budget_id, 'budget_state': reserved['state'],
                           'state': 'reservation_only', 'native_run': None}
                if attempt_path.is_file():
                    saved = _load_json(attempt_path)
                    grant = saved['grant']
                    require(saved['ordinal'] == ordinal and task and grant['task_id'] == task.id
                            and grant['board'] == board and grant['run_id'] in runs,
                            'stopped attempt escaped its original native run')
                    ended = runs[grant['run_id']]
                    require(ended.ended_at is not None, 'stopped native run has no end evidence')
                    outcome.update(state='attempt_without_completion',
                                   attempt=pinned(vault, attempt_path.relative_to(vault).as_posix()),
                                   native_run={'task_id': task.id, 'run_id': ended.id, 'outcome': ended.outcome})
                    if audit_path.is_file():
                        audit = _load_json(audit_path)
                        require(audit['grant'] == grant and audit['ordinal'] == ordinal
                                and audit['request_id'] == saved['request']['id'],
                                'stopped attempt audit identity changed')
                        outcome.update(state=audit['state'],
                                       audit=pinned(vault, audit_path.relative_to(vault).as_posix()))
                        if audit.get('model', {}).get('parent_native_run_id') == str(ended.id):
                            outcome['session_id'] = audit['model'].get('session_id')
                        if audit.get('error'):
                            outcome['error'] = audit['error']
                else:
                    require(not audit_path.exists(), 'stopped call audit has no attempt marker')
                outcomes.append(outcome)
            entry = {'state': 'isolated_attempt' if outcomes else 'unattempted',
                     'attempts': outcomes, 'task_id': task.id if task else None}
            if job.with_suffix('.result.json').exists():
                result, proof = verified_result(root, row)
                entry.update(state='acknowledged', native_state=result['state'], proof=proof)
            history[row['id']] = entry
        return history


def session_usage(history):
    """Read recorded usage for each attributable ordinal without equating absence to zero."""
    from workflow_metrics import usage_for

    attempts = [attempt for entry in history.values() for attempt in entry['attempts']]
    known = usage_for([{'model': {'session_id': row['session_id']}}
                       for row in attempts if row.get('session_id')], active=True)
    by_session = {row['session_id']: row for row in known['by_session']}
    for attempt in attempts:
        attempt['recorded_usage'] = by_session.get(attempt.get('session_id'))
    return known['known_totals']
