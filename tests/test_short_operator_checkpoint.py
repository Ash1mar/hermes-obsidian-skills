"""Verify short operator checkpoints using isolated plans and native bindings."""
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

import run_short_evidence_test as runner


class NativeBoard:
    def __init__(self):
        self.slug = None
        self.tasks = {}
        self.runs = {}

    def create_board(self, board, **_):
        assert self.slug is None
        self.slug = board

    def list_boards(self):
        return [{'slug': self.slug}] if self.slug else []

    def connect(self, *, board):
        assert board == self.slug
        return SimpleNamespace(close=lambda: None)

    def list_tasks(self, _conn, *, include_archived=False):
        return [task for task in self.tasks.values() if include_archived or task.status != 'archived']

    def get_task(self, _conn, task_id):
        return self.tasks.get(task_id)

    def list_runs(self, _conn, task_id, *, include_active):
        assert not include_active
        return self.runs[task_id]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    output = tmp_path / 'native'
    vault = tmp_path / 'vault'
    vault.mkdir()
    ledger = vault / 'origin.json'
    ledger.write_text('{}', encoding='utf-8')
    origin = {'workflow_id': 'source', 'path': str(ledger), 'sha256': runner.sha(ledger)}
    board = NativeBoard()
    calls = []
    phases = {'topic-seed': 'short-topics', 'map': 'short-map', 'coordination': 'short-coordinate', 'writing': 'short-write'}
    request_ids = {stage: str(i) * 64 for i, stage in enumerate(phases, 1)}

    def receipt_name(stage):
        suffix = '-section' if stage == 'writing' else ''
        return request_ids[stage] + suffix + '.json'

    def advance(*_args, **_kwargs):
        for stage, phase in phases.items():
            if not (output / 'receipts' / receipt_name(stage)).is_file():
                return {'stage': stage, 'requests': [{'id': request_ids[stage], 'phase': phase}]}
        return {'stage': 'offline_build'}

    def stage(root, operation, rows, capacity, **_):
        row, = rows
        request = row['id']
        calls.append(row['phase'])
        job = root / 'jobs' / (request + '.json')
        runner.save(job, {'request': row})
        native = SimpleNamespace(id=request, status='done', claim_lock=None,
                                 created_by=runner.CONTRACT, body=runner.task_body(root, job))
        board.tasks[request] = native
        board.runs[request] = [SimpleNamespace(id=1, outcome='completed')]
        stage_name = next(stage for stage, phase in phases.items() if phase == row['phase'])
        runner.save(root / 'receipts' / receipt_name(stage_name), {'phase': row['phase']})
        capture = {'sealed': ['section' if row['phase'] == 'short-write' else 'r']}
        runner.save(job.with_suffix('.result.json'), {'task_id': request, 'request_id': request,
                                                     'run_id': 1, 'attempts': [{'capture': capture}]})
        return [{'state': 'settled'}]

    profiles = ModuleType('hermes_cli.profiles')
    profiles.profile_exists = lambda _: False
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', profiles)
    monkeypatch.setattr(runner, 'source_boundary', lambda *_: ({}, origin))
    monkeypatch.setattr(runner, 'seed_requests', lambda *_: [])
    monkeypatch.setattr(runner, 'mapping_groups', lambda *_: {})
    monkeypatch.setattr(runner, 'workflow_processes', lambda *_: [])
    monkeypatch.setattr(runner, 'configuration', lambda: {'pass_worker_concurrency': 1, 'reduce_page_concurrency': 1})
    monkeypatch.setattr(runner, 'runtime', lambda: (board, board, None))
    monkeypatch.setattr(runner, 'advance', advance)
    monkeypatch.setattr(runner, 'stage', stage)
    monkeypatch.setattr(runner, 'stage_metrics', lambda *_: {})

    def run(pause_on=None, resume=None):
        def checkpoint(node, evidence):
            assert (output / 'operator-checkpoints' / (evidence['checkpoint_id'] + '.json')).is_file()
            return pause_on == node

        return runner.start(output, vault, 'source', [], 'checkpoints', page_contract='isolated-fixture',
                            checkpoint=checkpoint, continue_checkpoint=resume)

    return SimpleNamespace(run=run, calls=calls, board=board, request_ids=request_ids, output=output,
                           receipt=lambda stage: output / 'receipts' / receipt_name(stage))


def test_continue_reuses_earlier_native_work_and_accepts_archived_completion(pipeline):
    first = pipeline.run(pause_on='mapping')
    assert first['node'] == 'mapping'
    assert pipeline.calls == ['short-topics']
    pipeline.board.tasks[pipeline.request_ids['topic-seed']].status = 'archived'
    second = pipeline.run(pause_on='writing', resume=first['checkpoint_id'])
    assert second['node'] == 'writing'
    assert pipeline.calls == ['short-topics', 'short-map', 'short-coordinate']
    complete = pipeline.run(resume=second['checkpoint_id'])
    assert complete['last_stage'] == 'offline_build'
    assert pipeline.calls == ['short-topics', 'short-map', 'short-coordinate', 'short-write']
    assert runner.settled_checkpoint(pipeline.output)['result_sha256'] == runner.sha(pipeline.output / 'result.json')


def test_changed_or_unacknowledged_receipt_cannot_resume(pipeline):
    pause = pipeline.run(pause_on='mapping')
    receipt = pipeline.receipt('topic-seed')
    original = receipt.read_bytes()
    receipt.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='paused short-stage evidence'):
        pipeline.run(resume=pause['checkpoint_id'])
    receipt.write_bytes(original)
    execution_id = pipeline.request_ids['topic-seed']
    pipeline.board.tasks[execution_id].status = 'archived'
    pipeline.board.runs[execution_id][0].outcome = 'worker_exited'
    with pytest.raises(ValueError, match='acknowledged native run'):
        pipeline.run(resume=pause['checkpoint_id'])
    assert pipeline.calls == ['short-topics']
