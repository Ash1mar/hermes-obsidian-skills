"""Observe and control the current Hermes profile's Gateway."""

import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Lock, Thread
import time

from workflow import UserError, log_tail


SERVING = {"running", "degraded"}
OPERATION_TIMEOUT = 60
POLL_INTERVAL = 1
TRANSITION = {
    "start": ("starting", "正在等待 Hermes Gateway 就绪。"),
    "stop": ("draining", "已请求停止，正在等待 Gateway 退出。"),
}


class GatewayMonitor:
    def __init__(self, state, enabled, *, autostart=True):
        self.state = Path(state)
        self.autostart = autostart
        self.closed = Event()
        self.wake = Event()
        self.control_lock = Lock()
        self.pending = None
        self.error = None
        self.failed_action = None
        self.thread = None
        self.process = None
        self.logs = set()
        self.status = self.snapshot("checking" if enabled else "disabled")

    def start(self):
        if self.status["state"] != "disabled":
            self.thread = Thread(target=self.watch, daemon=True, name="hermes-gateway-monitor")
            self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        # The detached Gateway can continue after the HTTP server exits.
        if self.thread:
            self.thread.join(timeout=2)

    def request(self, action):
        with self.control_lock:
            if not self.status["can_" + action]:
                raise UserError("Gateway 当前状态不允许此操作，请刷新后重试。")
            self.pending = action
            self.error = None
            self.failed_action = None
            state, detail = TRANSITION[action]
            self.status = self.snapshot(state, detail=detail, pid=self.status["pid"])
            self.wake.set()
            return self.status

    def take_request(self):
        with self.control_lock:
            action = self.pending
            self.pending = None
            return action

    def snapshot(self, state, *, pid=None, ready=False, detail=None, can_start=False, can_stop=False):
        ownership = "started" if self.process and self.process.pid == pid else "reused" if pid else None
        return {"state": state, "pid": pid, "ready": ready, "detail": detail, "ownership": ownership,
                "can_start": can_start, "can_stop": can_stop, "operation_error": self.error}

    def report(self, state, **fields):
        with self.control_lock:
            # A poll started before the HTTP request must not overwrite acceptance.
            if self.pending is None:
                self.status = self.snapshot(state, **fields)

    def log(self):
        parts = [log_tail(self.state / name) for name in ("gateway-startup.log", "gateway-stop.log") if name in self.logs]
        return "\n\n".join(part for part in parts if part)

    def watch(self):
        try:
            from gateway.status import get_running_pid, read_runtime_status, resolve_gateway_liveness, runtime_status_is_stale
            from hermes_cli.config import load_config_readonly
            from hermes_constants import get_hermes_home
        except ImportError as error:
            self.report("failed", detail="无法加载 Hermes Gateway 的运行模块。")
            print(f"[Gateway] {error}", flush=True)
            return

        home = get_hermes_home().resolve()

        def observe():
            record = read_runtime_status(home / "gateway_state.json") or {}
            live = resolve_gateway_liveness(profile_dir=home, runtime=record,
                                            pid_probe=lambda path: get_running_pid(path, cleanup_stale=False))
            return live.runtime or record, live

        autostart = self.autostart
        while not self.closed.is_set():
            try:
                record, live = observe()
                action = self.take_request()
                if autostart and not live.running and not live.probe_error:
                    action = action or "start"
                autostart = False
                if action:
                    try:
                        self.run_operation(home, action, observe)
                    except (UserError, OSError, subprocess.TimeoutExpired) as error:
                        self.error = str(error)
                        self.failed_action = action
                        print(f"[Gateway {action}] {error}", flush=True)
                    record, live = observe()
                self.report_observation(record, live, load_config_readonly, runtime_status_is_stale)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                self.report("unverified", detail="暂时无法核对 Hermes Gateway 状态。")
                print(f"[Gateway] {error}", flush=True)
            self.wake.wait(POLL_INTERVAL)
            self.wake.clear()

    def report_observation(self, record, live, load_config, stale):
        pid = live.pid
        phase = record.get("gateway_state") if record.get("pid") == pid else None
        if ((self.failed_action == "start" and live.running and phase in SERVING)
                or (self.failed_action == "stop" and not live.running and not live.probe_error)):
            self.error = self.failed_action = None
        if live.running:
            shared = live.source == "multiplexer"
            fields = {"pid": pid, "can_stop": bool(pid) and not shared and phase not in {"starting", "draining"}}
            if phase in SERVING:
                dispatch = (load_config().get("kanban") or {}).get("dispatch_in_gateway", True)
                if self.process and self.process.pid == pid:
                    dispatch = dispatch and os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower() not in {
                        "0", "false", "no", "off"
                    }
                if not dispatch:
                    self.report("dispatch_disabled", detail="当前 Hermes 配置关闭了 Kanban 派发。", **fields)
                elif stale(record):
                    self.report("unverified", detail="Gateway 进程仍在，运行状态心跳已过期。", **fields)
                else:
                    detail = ("当前配置由共享 Gateway 提供服务。" if shared else
                              "Kanban 派发已启用。" if phase == "running" else
                              "Kanban 派发已启用，部分 Gateway 平台状态异常。")
                    self.report("ready" if phase == "running" else "degraded", ready=True, detail=detail, **fields)
            elif phase in {"starting", "draining"}:
                detail = "Hermes Gateway 正在启动。" if phase == "starting" else "Hermes Gateway 正在排空当前工作。"
                self.report(phase, detail=detail, **fields)
            elif phase == "startup_failed":
                self.report("failed", detail="Hermes 报告启动失败。", **fields)
            else:
                self.report("unverified", detail="Gateway 进程存在，暂未确认派发就绪。", **fields)
        elif live.probe_error:
            self.report("unverified", detail="Hermes 进程状态暂不可读。")
        elif self.process and self.process.poll() is None:
            self.report("unverified", pid=self.process.pid, detail="Gateway 启动进程仍在，暂未核实服务状态。")
        else:
            state = "failed" if self.failed_action == "start" else "stopped"
            self.report(state, detail="Gateway 当前未运行。", can_start=True)

    def run_operation(self, home, action, observe):
        import fcntl

        state, detail = TRANSITION[action]
        self.report(state, detail=detail, pid=self.status["pid"])
        with (home / ".operator-console-gateway.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UserError("另一个控制台正在操作同一 Gateway，请稍后查看状态。") from error
            _, live = observe()
            if live.probe_error:
                raise UserError("暂时无法核对 Gateway 进程，请稍后重试。")
            already_done = live.running if action == "start" else not live.running
            if already_done or self.closed.is_set():
                return
            if live.source == "multiplexer":
                raise UserError("当前配置由共享 Gateway 提供服务，请在其所属配置中操作。")
            if action == "start" and self.process and self.process.poll() is None:
                raise UserError("已有 Gateway 启动进程，请等待其退出或就绪。")
            deadline = time.monotonic() + OPERATION_TIMEOUT
            self.invoke(home, action)
            self.wait_for(action, observe, deadline)

    def invoke(self, home, action):
        executable = shutil.which("hermes")
        if not executable:
            raise UserError("找不到 Hermes 命令，请检查 WSL 的 PATH。")
        name = "gateway-startup.log" if action == "start" else "gateway-stop.log"
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state / name
        env = {**os.environ, "HERMES_HOME": str(home)}
        with path.open("w", encoding="utf-8") as log:
            path.chmod(0o600)
            self.logs.add(name)
            if action == "start":
                self.process = subprocess.Popen([executable, "gateway", "run"], env=env, stdin=subprocess.DEVNULL,
                                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            else:
                try:
                    result = subprocess.run([executable, "gateway", "stop"], env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT, timeout=OPERATION_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise UserError("停止命令等待超时，请查看 Gateway 日志和当前状态。") from error
                if result.returncode:
                    raise UserError(f"Hermes 未接受停止请求（退出码 {result.returncode}）。请查看 Gateway 日志。")

    def wait_for(self, action, observe, deadline):
        while not self.closed.is_set():
            record, live = observe()
            phase = record.get("gateway_state") if record.get("pid") == live.pid else None
            if action == "stop":
                if not live.running and not live.probe_error:
                    return
            else:
                if live.running and phase in SERVING:
                    return
                code = self.process.poll()
                if phase == "startup_failed" or (not live.running and not live.probe_error and code is not None):
                    detail = f"（退出码 {code}）" if code is not None else ""
                    raise UserError(f"Gateway 启动失败{detail}。请查看 Gateway 日志。")
            if time.monotonic() >= deadline:
                if action == "stop":
                    raise UserError("停止请求已发出，但尚未确认 Gateway 退出。")
                raise UserError("启动后尚未确认 Gateway 就绪。请查看 Gateway 日志。")
            self.closed.wait(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
