"""Start and observe Hermes Gateway for the local operator console."""

import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
import time

from workflow import log_tail


SERVING = {"running", "degraded"}
START_TIMEOUT = 60


class GatewayMonitor:
    def __init__(self, state, enabled):
        self.state = Path(state)
        self.stop = Event()
        self.thread = None
        self.process = None
        self.owns_log = False
        self.status = {"state": "checking" if enabled else "disabled", "ready": False}

    def start(self):
        if self.status["state"] != "disabled":
            self.thread = Thread(target=self.watch, daemon=True, name="hermes-gateway-monitor")
            self.thread.start()

    def close(self):
        self.stop.set()
        # The detached Gateway can still be serving a workflow after the HTTP server exits.
        if self.thread:
            self.thread.join(timeout=2)

    def log(self):
        return log_tail(self.state / "gateway-startup.log") if self.owns_log else ""

    def report(self, state, *, pid=None, ready=False, detail=None):
        self.status = {"state": state, "pid": pid, "ready": ready, "detail": detail,
                       "ownership": "started" if self.process and self.process.pid == pid
                       else "reused" if pid else None}

    def watch(self):
        lease = None
        try:
            from gateway.status import read_runtime_status, resolve_gateway_liveness, runtime_status_is_stale
            from hermes_cli.config import load_config_readonly
            from hermes_constants import get_hermes_home
            import fcntl

            home = get_hermes_home().resolve()

            def observe():
                record = read_runtime_status(home / "gateway_state.json") or {}
                live = resolve_gateway_liveness(profile_dir=home, runtime=record)
                return live.runtime or record, live

            record, live = observe()
            deadline = time.monotonic() + START_TIMEOUT
            if not live.running and not live.probe_error:
                # Serialize simultaneous console launches for the same Hermes profile.
                lease = (home / ".operator-console-gateway.lock").open("a")
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease.close()
                    lease = None
                else:
                    record, live = observe()
                    if not live.running and not live.probe_error and not self.stop.is_set():
                        self.launch()

            seen_serving = False
            while not self.stop.is_set():
                pid = live.pid
                phase = record.get("gateway_state") if record.get("pid") == pid else None
                exit_code = self.process.poll() if self.process else None
                waiting = time.monotonic() < deadline and not seen_serving
                if live.running:
                    if phase in SERVING:
                        seen_serving = True
                        config = load_config_readonly()
                        dispatch = (config.get("kanban") or {}).get("dispatch_in_gateway", True)
                        if self.process and self.process.pid == pid:
                            dispatch = dispatch and os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower() not in {
                                "0", "false", "no", "off"
                            }
                        heartbeat = not runtime_status_is_stale(record)
                        if not dispatch:
                            self.report("dispatch_disabled", pid=pid, detail="当前 Hermes 配置关闭了 Kanban 派发。")
                        elif not heartbeat:
                            self.report("unverified", pid=pid, detail="Gateway 进程仍在，运行状态心跳已过期。")
                        else:
                            self.report("ready" if phase == "running" else "degraded", pid=pid, ready=True,
                                        detail="Kanban 派发已启用。" if phase == "running"
                                        else "Kanban 派发已启用，部分 Gateway 平台状态异常。")
                    elif phase == "draining":
                        self.report("draining", pid=pid, detail="Gateway 正在排空当前工作。")
                    elif phase == "startup_failed":
                        self.report("failed", pid=pid, detail="Hermes 报告启动失败。")
                    elif waiting:
                        self.report("starting", pid=pid, detail="等待 Hermes 报告启动完成。")
                    else:
                        self.report("unverified", pid=pid, detail="Gateway 进程存在，暂未确认派发就绪。")
                elif live.probe_error:
                    self.report("unverified", detail="Hermes 进程状态暂不可读。")
                elif exit_code is not None and (exit_code != 0 or not seen_serving):
                    phase_label = "在就绪前退出" if not seen_serving else "异常退出"
                    self.report("failed", detail=f"Gateway {phase_label}，退出码 {exit_code}。")
                elif waiting:
                    self.report("starting", pid=self.process.pid if self.process else None,
                                detail="等待 Hermes 报告启动完成。")
                elif self.process and exit_code is None:
                    self.report("unverified", pid=self.process.pid, detail="启动超过 60 秒，仍在等待 Gateway 状态。")
                else:
                    self.report("stopped", detail="重启控制台可再次启动 Gateway。")
                if lease and (live.running or exit_code is not None or not waiting):
                    lease.close()
                    lease = None
                if self.stop.wait(2):
                    break
                record, live = observe()
        except (ImportError, OSError, ValueError, TypeError, AttributeError) as error:
            self.report("failed", detail=f"无法启动或读取 Hermes Gateway（{type(error).__name__}）。")
            print(f"[Gateway] {error}", flush=True)
        finally:
            if lease:
                lease.close()

    def launch(self):
        executable = shutil.which("hermes")
        if not executable:
            raise FileNotFoundError("hermes is not on PATH")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state / "gateway-startup.log"
        with path.open("w", encoding="utf-8") as log:
            path.chmod(0o600)
            self.owns_log = True
            self.process = subprocess.Popen([executable, "gateway", "run"], stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
