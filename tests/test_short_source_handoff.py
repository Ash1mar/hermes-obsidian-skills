"""The source observer uses native task state without starting a native worker."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/scripts'))
import start_post_mineru_short_test as launcher
import start_vault_workflow as startup
from hermes_source_units import FileIngestWorkflowService


def test_staged_bootstrap_and_reattach_use_one_native_workflow(tmp_path, monkeypatch):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'original content used only for the copy and identity checks')
    vault = tmp_path / 'vault'
    root = tmp_path / 'operation'
    root.mkdir()
    receipt = root / 'provisioning-receipt.json'
    request = {'vault_path': str(vault), 'vault_id': 'fixture', 'vault_name': 'Fixture',
               'security_domain': 'test', 'operation_id': 'fixture', 'actor': 'agent',
               'sources': [str(source)], 'test_mode': launcher.BOOTSTRAP_SHORT, 'pause_after': ['exact_plan'],
               'retrieval_policy': {'contract': 'vault-retrieval-policy/v1', 'sync_enabled': True,
                                    'query_enabled': False}}
    bootstrap = ROOT / 'hermes-obsidian-vault-bootstrap'
    calls = []

    def local_dispatch(path, command, payload):
        calls.append(command)
        return FileIngestWorkflowService(path).start(payload)

    monkeypatch.setattr(startup, 'dispatch', local_dispatch)
    staged = startup.start(request, receipt, bootstrap, prepare_only=True)
    assert staged['startup']['phase'] == 'sources_staged'
    assert staged['workflow'] is None and not calls
    assert (vault / '10_Raw/sample.pdf').read_bytes() == source.read_bytes()
    ledger = FileIngestWorkflowService(vault)._path('ingest-fixture')
    assert not ledger.exists()
    startup.start(request, receipt, bootstrap)
    assert calls == ['start']
    launcher.runner.save(root / 'provisioning-request.json', request)

    def observe(vault, request, ledger, receipt, update, previous):
        workflow = launcher._load_json(ledger)
        assert set(launcher.source_binding(vault, request, receipt, workflow)) == {'10_Raw/sample.pdf'}
        return workflow

    monkeypatch.setattr(launcher, 'wait_for_source', observe)
    monkeypatch.setattr(launcher, 'source_handoff', lambda *_: {'handoff': 'verified'})
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **kw: pytest.fail('attach attempted provisioning'))
    before = ledger.read_bytes()
    assert launcher.prepare(vault, request, root, None, None, attach=True) == {'handoff': 'verified'}
    assert before == ledger.read_bytes()
    assert launcher._load_json(root / 'source-wait.json')['state'] == 'handoff'
    with pytest.raises(ValueError, match='original provisioning request'):
        launcher.prepare(vault, {**request, 'vault_name': 'Changed'}, root, None, None, attach=True)


def test_working_file_can_exceed_observer_idle_window(monkeypatch):
    path = '10_Raw/sample.pdf'
    node = 'source-prepare:' + launcher.fingerprint({'path': path, 'content_sha256': 'hash'})[:16]
    workflow = {'workflow_id': 'ingest-fixture', 'revision': 2, 'scope': {'source_paths': [path]},
                'source_outcomes': [], 'state': 'source_preparing', 'current_stage': 'source_preparing',
                'cancel_requested': False, 'pause_control': {},
                'kanban': {'board_id': 'board-fixture', 'task_map': [{'node': node, 'task_id': 't_source'}]}}
    task = SimpleNamespace(id='t_source', status='running', current_run_id=1, claim_lock='active-claim')
    kb = SimpleNamespace(list_tasks=lambda _: [task])
    kc = SimpleNamespace(connect=lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(launcher.runner, 'runtime', lambda: (kb, kc, None))
    monkeypatch.setattr(launcher, '_load_json', lambda _: workflow)
    monkeypatch.setattr(launcher, 'source_binding', lambda *_: {path: 'hash'})
    clock = 0

    def sleep(_):
        nonlocal clock
        clock += 1_300
        if clock == 2_600:
            workflow.update(revision=3, source_outcomes=[{'path': path, 'status': 'ready'}],
                            current_stage='analyzing', pause_control={'boundary': 'exact_plan'})
            task.status, task.claim_lock = 'done', None

    monkeypatch.setattr(launcher, 'time', SimpleNamespace(monotonic=lambda: clock, time=lambda: clock, sleep=sleep))
    monkeypatch.setattr(launcher.runner, 'workflow_processes', lambda *_: [] if clock >= 2_600 else
                        [{'task_id': 't_source', 'kind': 'native_worker'}])
    snapshots = []
    assert launcher.wait_for_source(None, None, None, None, lambda **s: snapshots.append(s), []) is workflow
    assert snapshots[0]['files'][0]['status'] == 'running'
    assert snapshots[-1]['files'][0]['status'] == 'ready'
    assert clock == 2_600

    workflow['pause_control'] = {}
    del workflow['source_outcomes'][:]
    with pytest.raises(launcher.SourceWaitDetached, match='pending without observed native progress'):
        launcher.wait_for_source(None, None, None, None, lambda **_: None, [], idle_seconds=1)
