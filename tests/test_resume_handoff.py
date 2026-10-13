"""Resume handoff ordering with isolated adapters and no native processes."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))
sys.path.insert(0, str(ROOT / "hermes-obsidian-controlled-ingest/lib"))
sys.path.insert(0, str(ROOT / "hermes-obsidian-governed-ingest-orchestrator/lib"))

import orchestration
from hermes_source_units import ContractError
from hermes_source_units import runtime_telemetry


@pytest.fixture
def handoff(monkeypatch, tmp_path):
    events = []
    calls = []
    locks = []
    result = {
        "workflow_id": "ingest-resume-handoff",
        "background_dispatch": True,
        "program_dispatch": True,
        "state": "analyzing",
        "revision": 17,
    }
    state = SimpleNamespace(
        events=events, calls=calls, locks=locks, result=result,
        cli_error=None, cli_returncode=0, cli_stderr="", lock_busy=False,
        vault=tmp_path.resolve(),
    )

    class FakeAdapter:
        def __init__(self, vault, *, enable_workers):
            assert Path(vault) == state.vault
            assert enable_workers is True
            self.workflow = SimpleNamespace(vault=state.vault)

        def resume(self, request):
            events.append("resume-sync")
            return dict(result)

        def sync(self, request):
            events.append("sync")
            return dict(result)

        def continue_workflow(self, request):
            events.append("continue-sync")
            return dict(result)

    def run(argv, **kwargs):
        events.append("native-cli")
        calls.append((list(argv), kwargs))
        if state.cli_error is not None:
            raise state.cli_error
        return subprocess.CompletedProcess(
            argv, state.cli_returncode,
            json.dumps({"ok": state.cli_returncode == 0, "spawned": ["t_one", "t_two"]}),
            state.cli_stderr,
        )

    def popen(argv, **kwargs):
        events.append("watcher")
        calls.append((list(argv), kwargs))
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(pid=4242)

    @contextmanager
    def exclusive_lock(path, **kwargs):
        locks.append((Path(path), kwargs))
        events.append("lock-enter")
        if state.lock_busy:
            raise ContractError("LOCK_BUSY", "$", "existing watcher owns the lock")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(orchestration, "IngestKanbanAdapter", FakeAdapter)
    monkeypatch.setattr(orchestration, "configuration", lambda: {"worker_dispatch_enabled": True})
    # Replace this module's OS/subprocess handles, never global os.name or subprocess.
    monkeypatch.setattr(orchestration, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(orchestration, "subprocess", SimpleNamespace(
        run=run, Popen=popen, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setattr(orchestration, "_exclusive_lock", exclusive_lock, raising=False)
    monkeypatch.setattr(orchestration.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(runtime_telemetry, "runtime_directory", lambda *args: tmp_path / "telemetry")
    return state


def invoke(handoff, command="resume"):
    return orchestration.dispatch(handoff.vault, command, {
        "workflow_id": handoff.result["workflow_id"], "actor": "agent",
        "expected_revision": 16,
    })


def test_resume_handoff_dispatches_before_watcher_without_authority_snapshot(handoff):
    returned = invoke(handoff)
    assert handoff.events == ["resume-sync", "lock-enter", "native-cli", "lock-exit", "watcher"]
    assert returned["initial_program_dispatch"]["state"] == "dispatched"
    assert returned["reconciler_pid"] == 4242
    assert handoff.calls[0][0] == [
        "hermes", "ingest-program-dispatch", "--vault", str(handoff.vault),
        "--workflow-id", handoff.result["workflow_id"],
    ]
    assert handoff.calls[0][1]["capture_output"] is True
    assert handoff.calls[0][1]["text"] is True
    assert handoff.calls[0][1]["timeout"] == 60
    assert handoff.calls[1][0][1] == str(
        orchestration.SKILL / "scripts/watch_ingest_workflow.py"
    )
    assert handoff.locks[0][0] == handoff.vault / (
        "_system/ledgers/ingest-workflows/.watch-locks/ingest-resume-handoff.lock"
    )
    assert handoff.locks[0][1].get("timeout", 0) == 0


@pytest.mark.parametrize("background,program", [(False, True), (False, False), (True, False)])
def test_resume_handoff_skips_paused_or_nonprogram_work(handoff, background, program):
    handoff.result.update(background_dispatch=background, program_dispatch=program)
    if not background:
        handoff.result.update(state="paused", pause={"boundary": "pass", "evidence_digest": "held"})
    returned = invoke(handoff)
    assert "native-cli" not in handoff.events and not handoff.locks
    assert handoff.events == (["resume-sync", "watcher"] if background else ["resume-sync"])
    assert "initial_program_dispatch" not in returned
    if not background:
        assert returned["pause"]["boundary"] == "pass"
        assert "reconciler_pid" not in returned


@pytest.mark.parametrize("command", ["sync", "continue-workflow"])
def test_resume_handoff_does_not_run_for_other_commands(handoff, command):
    returned = invoke(handoff, command)
    assert "native-cli" not in handoff.events and not handoff.locks
    assert "initial_program_dispatch" not in returned
    assert ("watcher" in handoff.events) == (command == "continue-workflow")


@pytest.mark.parametrize("failure", ["nonzero", "oserror", "timeout"])
def test_resume_handoff_failure_defers_without_losing_watcher(handoff, failure):
    if failure == "nonzero":
        handoff.cli_returncode = 2
        handoff.cli_stderr = "native dispatch failed after possible partial claim"
    elif failure == "oserror":
        handoff.cli_error = OSError("native CLI unavailable")
    else:
        handoff.cli_error = subprocess.TimeoutExpired("hermes ingest-program-dispatch", 60)
    returned = invoke(handoff)
    receipt = returned["initial_program_dispatch"]
    assert receipt["state"] == "deferred"
    assert receipt["error"] and receipt["partial_spawn_possible"] is True
    assert handoff.events == ["resume-sync", "lock-enter", "native-cli", "lock-exit", "watcher"]
    assert returned["reconciler_pid"] == 4242
    assert sum(argv[0] == "hermes" for argv, _ in handoff.calls) == 1


def test_resume_handoff_busy_watcher_defers_without_second_dispatch(handoff):
    handoff.lock_busy = True
    returned = invoke(handoff)
    receipt = returned["initial_program_dispatch"]
    assert receipt["state"] == "deferred"
    assert receipt["error"] and receipt["partial_spawn_possible"] is True
    assert handoff.events == ["resume-sync", "lock-enter", "watcher"]
    assert len(handoff.calls) == 1 and handoff.calls[0][0][0] == sys.executable
    assert returned["reconciler_pid"] == 4242
