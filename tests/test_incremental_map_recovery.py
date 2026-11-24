"""Exercise Map recovery with the original native board and an isolated HTTP ledger."""
from contextlib import closing
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

import incremental_map_recovery as recovery
from incremental_native import NativeCompletionUnconfirmed
from model_transport import Journal
import run_short_evidence_test as native


@pytest.mark.parametrize('failure', ['precall', 'precall-released', 'precall-twice', 'sent'])
def test_map_drains_and_reuses_twenty_original_results(tmp_path, monkeypatch, failure):
    kb = pytest.importorskip('hermes_cli.kanban_db')
    kc = pytest.importorskip('hermes_cli.kanban_db_connect')
    kd = pytest.importorskip('hermes_cli.kanban_db_dispatch')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr(kb, '_fire_task_hook', lambda *_args, **_kwargs: None)
    import model_transport

    vault, run_id, source, board = tmp_path / 'vault', 'run', 'snapshot', 'map-original'
    run_root = vault / '_system/ledgers/incremental-ingest' / run_id
    root = run_root / 'native/source' / source / 'mapping'
    operation = {'contract': native.CONTRACT, 'workflow_mode': 'incremental_v1', 'vault': str(vault),
                 'board': board, 'pass_capacity': 10,
                 'incremental': {'run_id': run_id, 'scope_kind': 'source', 'scope_id': source, 'stage': 'mapping'}}
    rows = [{'id': f'{i:064x}', 'phase': 'short-map'} for i in range(1, 22)]
    kept, failed = rows[:-1], rows[-1]
    ledger = Journal(tmp_path / 'sends.sqlite3')
    monkeypatch.setattr(model_transport, 'journal_path', lambda: ledger.path)
    reserved = ({'outstanding': {'state': 'reserved', 'request_fingerprint': recovery.fingerprint(
        {'request': failed['id'], 'ordinal': 1})}} if failure == 'sent' else {})
    native.save(run_root / 'run.json', {'reservations': reserved})
    native.save(root / 'operation.json', operation)
    kb.create_board(board)

    def complete(connection, claim, job, request):
        result = {'request_id': request, 'state': 'settled', 'task_id': claim.id, 'run_id': claim.current_run_id}
        path = job.with_suffix('.result.json')
        native.save(path, result)
        assert kb.complete_task(connection, claim.id, expected_run_id=claim.current_run_id, fire_lifecycle_hook=False,
                                result=json.dumps({'state': result['state'], 'result_ref': str(path)}))

    def fail(connection, claim):
        assert kd._record_task_failure(
            connection, claim.id, 'sealed transient board startup failure',
            outcome=recovery.TRANSIENT_OUTCOME, force_trip=True, release_claim=True, end_run=True,
        )

    with closing(kc.connect(board=board)) as connection:
        tasks = {}
        for row in rows:
            job = root / 'jobs' / (row['id'] + '.json')
            native.save(job, {'request': row})
            tid = kb.create_task(connection, title='Short path short-map', body=native.task_body(root, job),
                                 assignee=native.ASSIGNEE, created_by=native.CONTRACT, workspace_kind='dir',
                                 workspace_path=str(root), idempotency_key=job.stem, board=board)
            claim = kb.claim_task(connection, tid, claimer=native.CONTRACT)
            tasks[row['id']] = tid, claim.current_run_id
            if row == failed:
                fail(connection, claim)
            else:
                complete(connection, claim, job, row['id'])
    failed_id, first_run = tasks[failed['id']]
    if failure == 'sent':
        evidence = json.dumps({'binding': {'board': board, 'task_id': failed_id, 'run_id': first_run}})
        with ledger.transaction() as connection:
            connection.execute('INSERT INTO sends VALUES (?, 1, ?, 1, 2, NULL, 0, ?)',
                               ('uncertain', 'route', evidence))

    def verified_result(workspace, row):
        result = json.loads((workspace / 'jobs' / (row['id'] + '.result.json')).read_bytes())
        with closing(kc.connect(board=board)) as connection:
            history = kb.list_runs(connection, result['task_id'], include_active=False)
            if not any(item.id == result['run_id'] and item.outcome == 'completed' for item in history):
                raise NativeCompletionUnconfirmed('test result has no original native acknowledgement')
        return result, {'task_id': result['task_id'], 'run_id': result['run_id']}

    monkeypatch.setattr(recovery, 'verified_result', verified_result)
    monkeypatch.setattr(recovery, 'active_admission', lambda _: True)
    monkeypatch.setattr(native, 'live', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(kd, 'configured_max_in_progress', lambda: None)
    monkeypatch.setattr(kd, '_memory_pressure_level', lambda: 'normal')
    monkeypatch.setattr(kd, '_restart_safe_worker_argv', lambda _claim, argv: argv)
    monkeypatch.setattr(kd, '_set_worker_pid', lambda *_args: None)
    admissions = []

    def simulate_worker(argv, *, env, **_options):
        tid = env['HERMES_KANBAN_TASK']
        job = Path(argv[-1])
        assert tid == failed_id and job.stem == failed['id']
        with closing(kc.connect(board=board)) as connection:
            claim = kb.get_task(connection, tid)
            assert claim.current_run_id != first_run and claim.claim_lock
            admissions.append((tid, claim.current_run_id))
            if failure == 'precall-twice':
                fail(connection, claim)
            else:
                complete(connection, claim, job, job.stem)
        exit_code = int(failure == 'precall-twice')
        return SimpleNamespace(pid=4321, returncode=exit_code, poll=lambda: exit_code,
                               wait=lambda **_: exit_code)

    monkeypatch.setattr(native.subprocess, 'Popen', simulate_worker)
    old_results = {row['id']: (root / 'jobs' / (row['id'] + '.result.json')).read_bytes() for row in kept}
    domain = SimpleNamespace(vault=vault)
    settled = []

    def settle_usage(_domain, _run_id, _workspace, row, result):
        settled.append((row['id'], result['task_id']))

    recovered = failure in ('precall', 'precall-released')
    if recovered:
        original_stage = native.stage

        def interrupted_before_claim(*_args, **_options):
            assert (root / 'map-retries' / (failed['id'] + '.json')).is_file()
            if failure == 'precall-released':
                with closing(kc.connect(board=board)) as connection:
                    assert kb.unblock_task(connection, failed_id)
            raise InterruptedError('controller restarted before the original task was claimed')

        with monkeypatch.context() as crashed:
            crashed.setattr(native, 'stage', interrupted_before_claim)
            with pytest.raises(InterruptedError):
                recovery.execute_map_rows(domain, run_id, root, operation, rows, settle_usage)
        assert native.stage is original_stage and admissions == []

    results, holds = recovery.execute_map_rows(domain, run_id, root, operation, rows, settle_usage)
    restarted = SimpleNamespace(vault=vault)
    assert recovery.execute_map_rows(restarted, run_id, root, operation, rows, settle_usage) == (results, holds)
    expected = rows if recovered else kept
    assert [row for row, _, _ in results] == expected
    expected_settlements = [(row['id'], tasks[row['id']][0]) for row in expected]
    assert settled == expected_settlements * 2
    assert all((root / 'jobs' / (key + '.result.json')).read_bytes() == value for key, value in old_results.items())
    with closing(kc.connect(board=board)) as connection:
        assert len(kb.list_tasks(connection)) == 21
        assert all(len(kb.list_runs(connection, tasks[row['id']][0], include_active=False)) == 1 for row in kept)
        original_history = kb.list_runs(connection, failed_id, include_active=False)
        assert len(original_history) == (1 if failure == 'sent' else 2)
    assert len(admissions) == (0 if failure == 'sent' else 1)
    if recovered:
        assert holds == {}
    else:
        assert holds[failed['id']]['task_id'] == failed_id
        assert (root / 'mapping-dispositions' / (failed['id'] + '.json')).is_file()
    assert json.loads((run_root / 'run.json').read_bytes())['reservations'] == reserved
    assert recovery.count_sends(operation, failed_id) == (1 if failure == 'sent' else 0)
