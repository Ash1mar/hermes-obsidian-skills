"""Isolated refill/cleanup ordering; no native processes or domain products."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))
sys.path.insert(0, str(ROOT / "hermes-obsidian-controlled-ingest/lib"))
sys.path.insert(0, str(ROOT / "hermes-obsidian-governed-ingest-orchestrator/lib"))

from hermes_source_units import ContractError, runtime_telemetry
import native_program_dispatch as dispatcher


@contextmanager
def no_span(*args, **kwargs):
    yield {}


def script_module(name):
    path = ROOT / "hermes-obsidian-governed-ingest-orchestrator/scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location("isolated_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_modules(monkeypatch, kb, kd, *, kw=None):
    class Connection:
        def close(self):
            pass

    hermes = ModuleType("hermes_cli")
    hermes.kanban_db = kb
    hermes.kanban_db_connect = SimpleNamespace(connect=lambda **kw: Connection())
    hermes.kanban_db_dispatch = kd
    hermes.kanban_db_workspace = kw or SimpleNamespace()
    profiles = ModuleType("hermes_cli.profiles")
    profiles.profile_exists = lambda name: False
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)


@pytest.fixture
def refill(monkeypatch, tmp_path):
    for name in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    events = []
    state = SimpleNamespace(events=events, pause=False, cancelled=False, blocked=set(), blocked_code="EXECUTION_BLOCKED")
    cards = [{"task_id": f"t{i}", "node": f"pass-slice:s{i}", "idempotency_key": f"key-{i}"}
             for i in range(1, 3)]
    tasks = [SimpleNamespace(id=card["task_id"], status="ready", assignee="ingest-program",
        body=json.dumps({"executor": "semantic-v1", "workflow_id": "ingest-refill", "worker_binding": card["task_id"]}),
        idempotency_key=card["idempotency_key"], current_run_id=100 + index,
        claim_lock=f"claim-{index}", worker_pid=None)
        for index, card in enumerate(cards)]

    def status(wid):
        assert wid == "ingest-refill"
        events.append("status")
        return {"workflow_id": wid, "cancel_requested": state.cancelled,
                "batch_id": "batch", "kanban": {"board_id": "board", "task_map": cards}}

    def fast(wid):
        events.append("fast-reconcile")
        if getattr(state, "pause_after_fast", False):
            state.pause = True
        if getattr(state, "cancel_after_fast", False):
            state.cancelled = True

    knowledge = SimpleNamespace(
        _batch=lambda _: {"slice_config": {"pass_worker_concurrency": 2}},
        _slices=lambda _: [{"slice_id": f"s{i}", "lease": {"worker_id": None}} for i in range(1, 3)],
    )
    state.adapter = SimpleNamespace(reconcile_pass_workers=fast, workflow=SimpleNamespace(
        vault=tmp_path, status=status, pending_pause=lambda _: state.pause, knowledge=knowledge))

    def list_tasks(conn):
        events.append("capacity-read")
        return tasks

    def claim(conn, task_id, **kwargs):
        events.append("claim:" + task_id)
        task = next(task for task in tasks if task.id == task_id)
        assert task.status == "ready"
        task.status = "running"
        return task

    kb = SimpleNamespace(list_tasks=list_tasks, claim_task=claim,
        _fire_worker_spawned_hook=lambda *a, **kw: None,
        kanban_db_path=lambda **kw: tmp_path / "native.db",
        workspaces_root=lambda **kw: tmp_path / "workspaces")
    kd = SimpleNamespace(reap_worker_zombies=lambda: events.append("reap"),
        configured_max_in_progress=lambda: None, resolve_max_in_progress=lambda _: None,
        _memory_pressure_level=lambda: "normal", check_respawn_guard=lambda *a: None,
        _restart_safe_worker_argv=lambda task, argv: argv,
        _open_worker_log=lambda *a: nullcontext(io.StringIO()),
        _set_worker_pid=lambda *a: None,
        _record_task_failure=lambda *a, **kw: pytest.fail("isolated spawn unexpectedly failed"))
    kw = SimpleNamespace(resolve_workspace=lambda *a, **kw: tmp_path, set_workspace_path=lambda *a: None)
    native_modules(monkeypatch, kb, kd, kw=kw)
    local = ModuleType("tools.environments.local")
    local.build_subprocess_env = lambda **kw: {}
    tools_package = ModuleType("tools")
    tools_package.__path__ = []
    environments_package = ModuleType("tools.environments")
    environments_package.__path__ = []
    tools_package.environments = environments_package
    environments_package.local = local
    gateway_package = ModuleType("gateway")
    gateway_package.__path__ = []
    gateway_context = ModuleType("gateway.session_context")
    gateway_context._VAR_MAP = {}
    gateway_package.session_context = gateway_context
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.environments", environments_package)
    monkeypatch.setitem(sys.modules, "tools.environments.local", local)
    monkeypatch.setitem(sys.modules, "gateway", gateway_package)
    monkeypatch.setitem(sys.modules, "gateway.session_context", gateway_context)
    monkeypatch.setattr(dispatcher, "subprocess", SimpleNamespace(
        Popen=lambda *a, **kw: events.append("spawn") or SimpleNamespace(pid=500),
        DEVNULL=-3, STDOUT=-2, CREATE_NO_WINDOW=0,
    ))
    monkeypatch.setattr(dispatcher, "span", no_span)
    monkeypatch.setattr(runtime_telemetry, "span", no_span)

    def canonical(adapter, binding, **kwargs):
        card = next(card for card in cards if card["task_id"] == binding)
        return {"task_id": card["task_id"], "node": card["node"]}, status("ingest-refill")

    def admission(adapter, request):
        events.append("admission:" + request["task_id"])
        if request["task_id"] in state.blocked:
            raise ContractError(state.blocked_code, "$", "deferred affected binding")
        return {"ok": True}

    monkeypatch.setattr(dispatcher, "canonical_binding", canonical)
    monkeypatch.setattr(dispatcher, "admission_check", admission)
    return state


def test_native_refill_reconciles_after_reap_before_fresh_capacity(refill):
    spawned = dispatcher.dispatch_programs(refill.adapter, "ingest-refill")
    assert spawned == ["t1", "t2"]
    events = refill.events
    assert events.index("reap") < events.index("fast-reconcile") < events.index("capacity-read")
    assert events[events.index("fast-reconcile") + 1] == "status"
    assert events.index("admission:t1") < events.index("claim:t1") < events.index("spawn")
    assert sum(item.startswith("claim:") for item in events) == 2


@pytest.mark.parametrize("change", ["pause", "cancel"])
def test_native_refill_rechecks_pause_and_cancel_after_fast_reconcile(refill, change):
    setattr(refill, change + "_after_fast", True)
    assert dispatcher.dispatch_programs(refill.adapter, "ingest-refill") == []
    assert "fast-reconcile" in refill.events
    assert "capacity-read" not in refill.events
    assert not any(item.startswith("claim:") for item in refill.events)


@pytest.mark.parametrize("code", ["EXECUTION_BLOCKED", "PASS_ADMISSION_WAIT"])
def test_native_refill_failed_first_binding_does_not_obstruct_next(refill, code):
    refill.blocked.add("t1")
    refill.blocked_code = code
    assert dispatcher.dispatch_programs(refill.adapter, "ingest-refill") == ["t2"]
    assert "admission:t1" in refill.events and "claim:t1" not in refill.events
    assert "claim:t2" in refill.events


@pytest.mark.parametrize("handoff_error,post_dispatch", [(False, False), (True, False), (False, True)])
def test_watcher_refills_before_full_sync_and_retains_wake(monkeypatch, tmp_path, handoff_error, post_dispatch):
    watcher = script_module("watch_ingest_workflow")
    events = []

    class Wake:
        def __init__(self):
            self.set = True

        def clear(self):
            events.append("clear")
            self.set = False

        def is_set(self):
            return self.set

        def wait(self, delay):
            events.append("wait")
            assert self.set, "wake received during sync must prevent sleep"

    wake = Wake()
    reads = []

    class Adapter:
        def __init__(self, vault, *, enable_workers):
            self.workflow = SimpleNamespace(vault=tmp_path, status=self.status)

        def status(self, wid):
            reads.append(wid)
            return {"cancel_requested": len(reads) > 1, "state": "analyzing"}

        def _sync_current(self, wid, *, max_attempts, yield_requested):
            assert max_attempts == 1 and not yield_requested()
            events.append("full-sync")
            wake.set = True
            return {"background_dispatch": post_dispatch, "program_dispatch": post_dispatch,
                    "revision": 1, "state": "analyzing"}

    def ready(vault, wid):
        events.append("ready-dispatch")
        if handoff_error:
            raise OSError("isolated handoff unavailable")

    monkeypatch.setattr(watcher, "IngestKanbanAdapter", Adapter)
    monkeypatch.setattr(watcher, "wake_events", lambda *a: nullcontext(wake))
    monkeypatch.setattr(watcher, "_exclusive_lock", lambda *a: nullcontext())
    monkeypatch.setattr(watcher, "dispatch_ready_programs", ready)
    monkeypatch.setattr(watcher, "span", no_span)
    watcher.watch(str(tmp_path), "ingest-refill")
    assert events == ["clear", "ready-dispatch", "full-sync",
                      *(["ready-dispatch"] if post_dispatch else []), "wait", "clear"]


@pytest.fixture
def worker(monkeypatch, tmp_path):
    module = script_module("run_program_worker")
    events = []
    task = SimpleNamespace(status="running", current_run_id=17, claim_lock="claim-17", worker_pid=os.getpid())
    request = {"workflow_id": "ingest-refill", "task_id": "t1", "node": "pass-slice:s1"}
    value = {"kanban": {"board_id": "board"}}
    original = ContractError("ISOLATED_ORIGINAL", "$", "preserve this failure")
    state = SimpleNamespace(module=module, events=events, task=task, original=original,
                            report_error=False, succeed=False, writes=[])

    class Adapter:
        def __init__(self, *a, **kw):
            pass

        def execution_failure(self, req, code, message):
            events.append("failure-report")
            if state.report_error:
                raise OSError("isolated reporting unavailable")
            return "_system/reports/failure.json"

    class FakeThread:
        def __init__(self, **kw):
            pass

        def start(self):
            events.append("heartbeat-start")

        def join(self, **kw):
            events.append("heartbeat-joined")

    def handler(signum, fn):
        events.append("handler-change")
        return "prior-handler"

    def execute(adapter, binding):
        events.append("execute")
        if state.succeed:
            return {"ok": True}
        raise original

    def wake(vault, wid):
        events.append("wake")
        if state.succeed:
            # Simulate trusted completion acknowledgement, not worker mutation.
            task.status = "done"

    def write(path, data):
        state.writes.append(json.loads(data))

    native_modules(monkeypatch, SimpleNamespace(get_task=lambda *a: task), SimpleNamespace(
        _record_task_failure=lambda *a, **kw: pytest.fail("worker must not use a non-CAS private failure helper")))
    monkeypatch.setenv("HERMES_INGEST_EXECUTOR", "program-v1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-17")
    monkeypatch.setattr(module, "IngestKanbanAdapter", Adapter)
    monkeypatch.setattr(module, "canonical_binding", lambda *a, **kw: (request, value))
    monkeypatch.setattr(module, "execute", execute)
    monkeypatch.setattr(module, "_write_atomic", write)
    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=FakeThread, Event=threading.Event))
    monkeypatch.setattr(module, "signal", SimpleNamespace(SIGTERM=15, signal=handler))
    monkeypatch.setattr(module, "notify_reconciler", wake)
    monkeypatch.setattr(module, "span", no_span)
    monkeypatch.setattr(runtime_telemetry, "span", no_span)
    state.binding = tmp_path / "immutable-binding.json"
    return state


@pytest.mark.parametrize("report_error", [False, True])
def test_worker_failed_cleanup_preserves_original_and_wakes_last(worker, report_error):
    worker.report_error = report_error
    with pytest.raises(ContractError) as raised:
        worker.module.run("isolated-vault", str(worker.binding))
    assert raised.value is worker.original
    assert worker.events[-3:] == ["heartbeat-joined", "handler-change", "wake"]
    assert worker.writes[0]["state"] == "running"
    assert worker.writes[0]["grant"] == {"claim_lock": "claim-17", "worker_pid": worker.task.worker_pid}
    if not report_error:
        assert worker.writes[-1]["state"] == "execution_blocked"


def test_worker_success_leaves_native_completion_to_trusted_ack_and_wakes_after_cleanup(worker):
    worker.succeed = True
    assert worker.module.run("isolated-vault", str(worker.binding)) == {"ok": True}
    assert "failure-report" not in worker.events
    assert worker.events.count("wake") == 2
    assert worker.events[-3:] == ["heartbeat-joined", "handler-change", "wake"]


def test_worker_progress_records_native_registered_pid_without_assuming_child_pid(worker):
    worker.task.worker_pid += 1000  # A native wrapper may own the registered PID.
    with pytest.raises(ContractError) as raised:
        worker.module.run("isolated-vault", str(worker.binding))
    assert raised.value is worker.original
    assert all(record["grant"]["worker_pid"] == worker.task.worker_pid for record in worker.writes)


@pytest.mark.parametrize("changed", ["run", "claim", "pid_missing"])
def test_worker_foreign_native_grant_cannot_begin_or_write(worker, monkeypatch, changed):
    if changed == "run":
        worker.task.current_run_id = 18
    elif changed == "claim":
        worker.task.claim_lock = "foreign-claim"
    else:
        worker.task.worker_pid = None
    ticks = iter([0, 11])
    monkeypatch.setattr(worker.module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="registration missing or superseded"):
        worker.module.run("isolated-vault", str(worker.binding))
    assert not worker.writes and "execute" not in worker.events
    assert "failure-report" not in worker.events
