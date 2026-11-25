"""Evidence required to treat a native preparation as an interrupted execution."""

import hashlib
import json

ABNORMAL_ENDS = frozenset({'failed', 'error', 'worker_exited', 'worker_crash', 'crashed', 'gave_up',
                           'timeout', 'timed_out', 'lease_expired', 'claim_expired', 'killed', 'cancelled',
                           'reclaimed', 'stale'})


def native_state(task, last):
    current = getattr(task, 'current_run_id', None)
    last_id = getattr(last, 'id', None)
    return {'status': task.status, 'claimed': bool(task.claim_lock or (current is not None and current != last_id)),
            'worker_pid': getattr(task, 'worker_pid', None), 'last_outcome': getattr(last, 'outcome', None),
            'last_run_id': last_id, 'last_started_at': getattr(last, 'started_at', None),
            'last_ended_at': getattr(last, 'ended_at', None)}


def interrupted_preparation(task, execution):
    """Match the ended native run to a worker execution, rather than a queue event."""
    grant = execution.get('grant') or {}
    return (task.get('status') not in ('running', 'ready', 'todo') and not task.get('claimed')
            and not task.get('worker_pid') and task.get('last_outcome') in ABNORMAL_ENDS
            and task.get('last_started_at') is not None and task.get('last_ended_at') is not None
            and type(task.get('last_run_id')) is int and execution.get('run_id') == task['last_run_id']
            and execution.get('executor') == 'program-v1' and execution.get('state') in ('running', 'execution_blocked')
            and type(grant.get('worker_pid')) is int and grant['worker_pid'] > 0 and bool(grant.get('claim_lock')))


def can_retry_preparation(*, run_state, control_state, admission, source_state, outcome, task, execution,
                          needs_review=False):
    return (run_state == control_state == 'active' and admission != 'held' and source_state == 'pending'
            and not outcome and not needs_review and task.get('status') in ('blocked', 'scheduled')
            and interrupted_preparation(task, execution))


def startup_failure_evidence(task, runs, log):
    """Correlate one pre-registration import failure with its original native run."""
    if (task.status not in ('archived', 'blocked', 'scheduled') or task.claim_lock
            or task.current_run_id is not None or task.worker_pid or not runs or not log):
        return None
    if any(row.ended_at is None or row.outcome == 'completed' for row in runs):
        return None
    attempted = [row for row in runs if row.worker_pid]
    if len(attempted) != 1:
        return None
    ended = attempted[0]
    if (ended.id != max(row.id for row in runs) or ended.outcome not in ABNORMAL_ENDS | {'scheduled'}
            or type(ended.worker_pid) is not int or ended.worker_pid <= 0
            or ended.started_at is None or ended.started_at > ended.ended_at):
        return None
    try:
        diagnostic = json.loads(log)
    except (TypeError, ValueError):
        return None
    if diagnostic != {'ok': False, 'code': 'PROGRAM_EXECUTION_FAILED', 'error': "No module named 'hermes_cli'"}:
        return None
    return {'run_id': ended.id, 'outcome': ended.outcome, 'worker_pid': ended.worker_pid,
            'started_at': ended.started_at, 'ended_at': ended.ended_at,
            'log_sha256': hashlib.sha256(log.encode('utf-8')).hexdigest()}
