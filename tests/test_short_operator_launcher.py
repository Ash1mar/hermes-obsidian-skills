"""Isolate console authorization and reuse from native model execution."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/scripts'


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import start_post_mineru_short_test as launch

    if os.name != 'posix':
        # These fixtures exercise continuation semantics in one process.
        monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=lambda *_: None, LOCK_EX=2))
    root = tmp_path / 'short'
    root.mkdir()
    vault = tmp_path / 'vault'
    request = {'operation_id': 'test', 'actor': 'operator', 'test_mode': launch.BOOTSTRAP_SHORT,
               'security_domain': 'internal-test'}
    args = SimpleNamespace(request=tmp_path / 'request.json', provider_config=tmp_path / 'provider.json',
                           provider_state=tmp_path / 'index', provisioner_skill=SCRIPTS.parent,
                           bootstrap_skill=SCRIPTS.parent, operator_attempt='first', attach_source=False,
                           continue_operation=False, pause_before=None)
    launch.runner.save(args.request, request)
    launch.runner.save(args.provider_config, {})
    ledger = vault / '_system/ledgers/ingest-workflows/ingest-test.json'
    calls = []
    monkeypatch.setitem(sys.modules, 'ingest_kanban', SimpleNamespace(KanbanCLI=lambda: SimpleNamespace(
        dispatcher_available=lambda: True)))

    def prepare(*_, attach, checkpoint):
        if not attach:
            if checkpoint('prepare', {'sources': 'staged'}):
                return None
            calls.append('dispatch')
            launch.runner.save(ledger, {})
        if checkpoint('exact', {'handoff': 'same'}):
            return None
        return {'workflow_id': 'ingest-test', 'pins': ['one']}

    def start(short, *_, checkpoint, continue_checkpoint):
        stages = ['topics', 'mapping', 'coordination', 'writing']
        stages = stages[stages.index(continue_checkpoint):] if continue_checkpoint else stages
        for node in stages:
            if checkpoint(node, {'checkpoint_id': node}):
                return {'state': 'paused'}
            calls.append(node)
        launch.runner.save(short / 'operation.json', {})
        launch.runner.save(short / 'result.json', {'state': 'completed'})
        launch.runner.save(short / 'stage-05-offline_build.json', {})
        return {'state': 'completed'}

    def build(*_, before_delivery):
        if not (root / 'build.json').exists():
            calls.append('formal-build')
            launch.runner.save(root / 'build.json', {})
        if before_delivery('release', {'proof': 'same'}):
            return None
        if not (root / 'provider-result.json').exists():
            calls.append('release')
        return {'release_id': 'release'}

    def sync(_vault, output, release, _config):
        calls.append('sync')
        index = {'status': 'ready', 'release_id': release}
        launch.runner.save(output / 'provider-result.json', {'returncode': 0, 'index': index})
        return index

    monkeypatch.setattr(launch, 'prepare', prepare)
    monkeypatch.setattr(launch, 'qualify_sources', lambda *_: {'ok': True, 'resolved': 1})
    monkeypatch.setattr(launch.runner, 'start', start)
    monkeypatch.setattr(launch.runner, 'settled_checkpoint', lambda _: {'plan_sha256': 'same'})
    monkeypatch.setattr(launch, 'delivery_partition', lambda _: {'included': ['page']})
    monkeypatch.setattr(launch, 'final_plan', lambda *_: None)
    monkeypatch.setattr(launch, 'execute', build)
    monkeypatch.setattr(launch, 'synchronize', sync)
    monkeypatch.setattr(launch, 'accept', lambda *_: calls.append('acceptance') or {'release': 'ok'})
    return launch, args, request, root, vault, calls


@pytest.mark.parametrize('node', ('bootstrap', 'prepare', 'exact', 'topics', 'mapping', 'coordination',
                                  'writing', 'formal-build', 'release', 'acceptance'))
def test_continue_reuses_upstream_work_at_every_node(console, node):
    launch, args, request, root, vault, calls = console
    args.pause_before = node
    launch.run_controlled(args, request, root, vault)
    state_path = root / 'operator-state.json'
    state = launch._load_json(state_path)
    assert state['state'] == 'paused' and state['node'] == node
    assert ('formal-build' in calls) == (node in ('release', 'acceptance'))
    assert ('sync' in calls) == (node == 'acceptance')
    assert not (root / 'final-acceptance.json').exists()

    args.operator_attempt, args.continue_operation, args.pause_before = 'second', True, None
    launch.run_controlled(args, request, root, vault)
    assert calls == ['dispatch', 'topics', 'mapping', 'coordination', 'writing', 'formal-build',
                     'release', 'sync', 'acceptance']
    assert launch._load_json(state_path)['state'] == 'completed'


def test_invalid_continue_preserves_pause(console):
    launch, args, request, root, vault, calls = console
    args.pause_before = 'prepare'
    launch.run_controlled(args, request, root, vault)
    state = (root / 'operator-state.json').read_bytes()
    args.operator_attempt, args.continue_operation, args.pause_before = 'second', True, None
    args.provider_config.write_text('{"changed":true}', encoding='utf-8')
    with pytest.raises(ValueError, match='original console request'):
        launch.run_controlled(args, request, root, vault)
    assert (root / 'operator-state.json').read_bytes() == state
    assert not calls
