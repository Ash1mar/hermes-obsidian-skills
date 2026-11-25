"""Isolated refill/cleanup ordering; no native processes or domain products."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = Path(os.environ.get('HERMES_TEST_ORCHESTRATOR_SKILL',
                                  ROOT / 'hermes-obsidian-governed-ingest-orchestrator')).resolve()
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))
sys.path.insert(0, str(ROOT / "hermes-obsidian-controlled-ingest/lib"))
sys.path.insert(0, str(ORCHESTRATOR / 'lib'))

from hermes_source_units import ContractError, runtime_telemetry
import native_program_dispatch as dispatcher


@contextmanager
def no_span(*args, **kwargs):
    yield {}


def script_module(name):
    path = ORCHESTRATOR / 'scripts' / f'{name}.py'
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
    state.cards, state.tasks = cards, tasks

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
    state.adapter = SimpleNamespace(program_worker_concurrency=2, reconcile_pass_workers=fast, workflow=SimpleNamespace(
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
        count_running_tasks=lambda conn: sum(t.status == 'running' for t in tasks),
        count_running_tasks_other_boards=lambda board: 0,
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
    state.worker_python = tmp_path / "managed" / "bin" / "python3"
    state.worker_launches = []
    monkeypatch.setattr(dispatcher, "worker_runtime", lambda: (state.worker_python, {}))

    def spawn(argv, **kwargs):
        events.append("spawn")
        state.worker_launches.append(argv)
        return SimpleNamespace(pid=500)

    monkeypatch.setattr(dispatcher, "subprocess", SimpleNamespace(
        Popen=spawn, DEVNULL=-3, STDOUT=-2, CREATE_NO_WINDOW=0,
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
    assert [argv[0] for argv in refill.worker_launches] == [str(refill.worker_python)] * 2


def test_runtime_unavailable_before_any_native_claim(refill, monkeypatch):
    def unavailable():
        raise ContractError('PROGRAM_RUNTIME_UNAVAILABLE', '$', 'installed interpreter cannot import Hermes')

    monkeypatch.setattr(dispatcher, 'worker_runtime', unavailable)
    with pytest.raises(ContractError) as stopped:
        dispatcher.dispatch_programs(refill.adapter, 'ingest-refill')

    assert stopped.value.code == 'PROGRAM_RUNTIME_UNAVAILABLE'
    assert all(task.status == 'ready' for task in refill.tasks)
    assert not any(event.startswith('claim:') for event in refill.events)
    assert not refill.worker_launches


def test_reduce_dispatch_cap_two_even_when_native_config_is_unbounded(refill):
    from native_program_dispatch import effective_capacity
    assert effective_capacity(refill.adapter, None) == 2
    assert effective_capacity(refill.adapter, 8) == 2
    assert effective_capacity(refill.adapter, 1) == 1
    for card, task in zip(refill.cards, refill.tasks):
        card['node'] = 'resource-reduce:' + task.id
    extra = dict(refill.cards[-1], task_id='t3', node='resource-reduce:t3', idempotency_key='key-3')
    refill.cards.append(extra)
    task = copy_task = SimpleNamespace(**vars(refill.tasks[-1]))
    copy_task.id, copy_task.idempotency_key = 't3', 'key-3'
    copy_task.body = json.dumps({'executor':'semantic-v1', 'workflow_id':'ingest-refill', 'worker_binding':'t3'})
    refill.tasks.append(task)
    assert dispatcher.dispatch_programs(refill.adapter, 'ingest-refill') == ['t1','t2']
    assert refill.tasks[-1].status == 'ready'


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


@pytest.mark.parametrize("handoff_error,post_dispatch", [(None, False), ('before', False), (None, True), ('after', True)])
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
            return {"cancel_requested": len(reads) > 1, "state": "analyzing", "current_stage":"analyzing",
                    "scope": {}, "revision": 1}

        def _sync_current(self, wid, *, max_attempts, yield_requested):
            assert max_attempts == 1 and not yield_requested()
            events.append("full-sync")
            wake.set = True
            return {"background_dispatch": post_dispatch, "program_dispatch": post_dispatch,
                    "revision": 1, "state": "analyzing"}

    def ready(vault, wid):
        events.append("ready-dispatch")
        phase = 'after' if 'full-sync' in events else 'before'
        if handoff_error == phase:
            raise OSError("isolated handoff unavailable")

    monkeypatch.setattr(watcher, "IngestKanbanAdapter", Adapter)
    monkeypatch.setattr(watcher, "wake_events", lambda *a: nullcontext(wake))
    monkeypatch.setattr(watcher, "_exclusive_lock", lambda *a: nullcontext())
    monkeypatch.setattr(watcher, "dispatch_ready_programs", ready)
    monkeypatch.setattr(watcher, "span", no_span)
    if handoff_error == 'after':
        with pytest.raises(OSError, match='isolated handoff unavailable'):
            watcher.watch(str(tmp_path), 'ingest-refill')
        assert events == ['clear', 'ready-dispatch', 'full-sync', 'ready-dispatch']
        return
    watcher.watch(str(tmp_path), "ingest-refill")
    assert events == ["clear", "ready-dispatch", "full-sync",
                      *(["ready-dispatch"] if post_dispatch else []), "wait", "clear"]


@pytest.mark.parametrize('busy_attempt', [1, 2], ids=['initial-handoff', 'after-full-sync'])
def test_incremental_source_refill_retries_exact_stderr_lock_without_replacing_work(refill, monkeypatch, tmp_path, busy_attempt):
    import citation_native_authority
    import orchestration

    watcher = script_module('watch_ingest_workflow')
    for module in (watcher, orchestration, dispatcher):
        assert Path(module.__file__).resolve().parents[1] == ORCHESTRATOR
    wid = 'ingest-refill'
    task, card = refill.tasks[0], refill.cards[0]
    refill.tasks[:] = [task]
    refill.cards[:] = [card]
    card['node'] = 'source-prepare:original-source'
    original_card = dict(card)
    task.status, task.current_run_id, task.claim_lock = 'scheduled', None, None
    task.body = json.dumps({'executor': 'program-v1', 'workflow_id': wid, 'worker_binding': task.id})
    original_body = task.body
    native_db = sys.modules['hermes_cli'].kanban_db
    claim_task = native_db.claim_task

    def claim_original(connection, task_id, **options):
        claimed = claim_task(connection, task_id, **options)
        assert claimed is task and claimed.claim_lock is None
        claimed.current_run_id, claimed.claim_lock = 101, 'original-claim'
        return claimed

    monkeypatch.setattr(native_db, 'claim_task', claim_original)
    read_native = refill.adapter.workflow.status

    ledger = tmp_path / '_system/ledgers/ingest-workflows' / (wid + '.json')
    ledger.parent.mkdir(parents=True)
    value = {**read_native(wid), 'revision': 1, 'current_stage': 'preparing', 'state': 'preparing',
             'scope': {'workflow_mode': 'incremental_v1'}}
    ledger.write_text(json.dumps(value), encoding='utf-8')
    prior_map = tmp_path / '_system/reports/incremental-ingest/original-run/map-results/original-map.json'
    prior_map.parent.mkdir(parents=True)
    receipt = b'{"map_result_id":"original-map","snapshot_id":"verified-earlier-source"}\n'
    prior_map.write_bytes(receipt)
    reads, syncs, handoffs, waits = [], [], [], []
    wake = threading.Event()

    def status(workflow_id):
        assert workflow_id == wid
        current = json.loads(ledger.read_bytes())
        assert current['workflow_id'] == wid and current['kanban']['task_map'] == [original_card]
        reads.append((current['revision'], task.status))
        assert len(reads) < 6, 'watcher should exit after the original source completes'
        return current

    def sync(workflow_id, *, max_attempts, yield_requested):
        assert workflow_id == wid and max_attempts == 1 and not yield_requested()
        assert task.status == 'scheduled' and not syncs
        syncs.append(1)
        task.status = 'ready'
        ledger.write_text(json.dumps({**value, 'revision': 2}), encoding='utf-8')
        return {'revision': 2, 'state': 'preparing', 'program_dispatch': True, 'background_dispatch': True}

    def wait(interval):
        if task.status == 'done':
            assert wake.is_set()
            return True
        assert task.claim_lock is None and 'claim:' + task.id not in refill.events and prior_map.read_bytes() == receipt
        waits.append((interval, task.status, len(reads)))
        return False

    def handoff(argv, **options):
        assert argv == ['hermes', 'ingest-program-dispatch', '--vault', str(tmp_path), '--workflow-id', wid]
        assert options['capture_output'] and options['text']
        handoffs.append(task.status)
        if len(handoffs) == busy_attempt:
            lock = ledger.parent / '.dispatch-locks' / (wid + '.lock')
            stderr = json.dumps({'ok': False, 'error': f'LOCK_BUSY at $: Skill kernel lock is held: {lock}; wait limit=0s'})
            return subprocess.CompletedProcess(argv, 2, stdout='', stderr=stderr + '\n')
        claimed = dispatcher.dispatch_programs(refill.adapter, wid)
        if claimed:
            assert claimed == [original_card['task_id']] and task.body == original_body
            task.status, task.claim_lock = 'done', None
            wake.set()
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'ok': True, 'claimed': claimed}), stderr='')

    adapter = SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path, status=status,
        source_coverage=lambda _: {'pending': task.status != 'done'}), _sync_current=sync)
    monkeypatch.setattr(watcher, 'IngestKanbanAdapter', lambda *_a, **_kw: adapter)
    monkeypatch.setattr(watcher, 'wake_events', lambda *_: nullcontext(wake))
    monkeypatch.setattr(wake, 'wait', wait)
    monkeypatch.setattr(watcher, 'span', no_span)
    monkeypatch.setattr(orchestration, 'IngestKanbanAdapter', lambda *_: refill.adapter)
    monkeypatch.setattr(citation_native_authority, 'capture', lambda *_: None)
    monkeypatch.setattr(orchestration, 'span', no_span)
    monkeypatch.setattr(orchestration.subprocess, 'run', handoff)

    interval = 2.25
    watcher.watch(str(tmp_path), wid, interval)
    expected = (1, 'scheduled') if busy_attempt == 1 else (2, 'ready')
    assert len(waits) == 1 and waits[0][:2] == (interval, expected[1])
    assert reads[waits[0][2]] == expected
    assert len(handoffs) == 3 and syncs == [1] and reads[-1] == (2, 'done')
    assert [item for item in refill.events if item.startswith('claim:')] == ['claim:' + original_card['task_id']]
    assert refill.events.count('spawn') == 1 and original_card == card and prior_map.read_bytes() == receipt


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
    state.binding.write_text(json.dumps(request))
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
