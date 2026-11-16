"""Serve the loopback-only Hermes operator console (Python 3.11+)."""

import argparse
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from workflow import (FINALIZE, UserError, attempts_for, local_path, log_tail,
                      new_request, progress, read, run_attempt, save)


HERE = Path(__file__).resolve().parent
RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,70}")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css")}


class Console:
    def __init__(self, config, histories):
        self.config = config
        self.state = Path(config["state"])
        self.history = {}
        for directory in histories:
            request = read(directory / "request.json")
            if not RUN_ID.fullmatch(request.get("operation_id", "")) or not (directory / "short").is_dir():
                raise UserError(f"找不到现有短路径报告：{directory}")
            self.history[request["operation_id"]] = directory

    def find(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise UserError("无效的运行编号。")
        directory = self.state / run_id
        if (directory / "request.json").is_file():
            return directory, False
        if run_id in self.history:
            return self.history[run_id], True
        raise FileNotFoundError(run_id)

    def lock(self):
        import fcntl

        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = (self.state / "dispatch.lock").open("a")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            return None
        return lease

    def is_active(self, directory, historical):
        if historical or not self.config["writable"]:
            return False
        attempts = attempts_for(directory)
        if not attempts or "returncode" in attempts[-1]:
            return False
        lease = self.lock()
        if lease is not None:
            lease.close()
            return False
        return read(self.state / "active.json") == {"run": directory.name, "attempt": attempts[-1]["id"]}

    def list_runs(self):
        directories = {p.parent.name: (p.parent, False) for p in self.state.glob("*/request.json")}
        for key, path in self.history.items():
            directories.setdefault(key, (path, True))
        rows = [progress(path, historical=historical, active=self.is_active(path, historical), details=False)
                for path, historical in directories.values()]
        return sorted(rows, key=lambda row: (row["started_at"] or 0, row["id"]), reverse=True)

    def launch(self, body, run_id=None):
        if not self.config["writable"]:
            raise UserError("当前仅开放历史查看。请在 Hermes 所在的 WSL 中运行控制台。")
        lease = self.lock()
        if lease is None:
            raise UserError("已有控制台任务正在运行，请等待本轮退出后再提交。")
        with closing(lease):
            manifest = None
            if run_id:
                directory, historical = self.find(run_id)
                action = body.get("action")
                prior_entry = body.get("prior_entry")
                snapshot = progress(directory, historical=historical)
                actions = [item for node in snapshot["nodes"] for item in node["actions"]]
                option = next((item for item in actions if item["id"] == action
                               and item.get("prior_entry") == prior_entry), None)
                if option is None:
                    raise UserError("当前回执不支持此操作。请刷新并检查节点详情。")
                if option.get("manifest"):
                    manifest_path = local_path(body.get("manifest"), self.config["distro"]).resolve()
                    if not manifest_path.is_file() or manifest_path.stat().st_size > 262144:
                        raise UserError("请输入有效的映射恢复 JSON 清单。")
                    try:
                        manifest = read(manifest_path)
                    except (UnicodeError, json.JSONDecodeError) as error:
                        raise UserError("恢复清单必须是 UTF-8 JSON。") from error
                    if (not isinstance(manifest, dict) or set(manifest) != {"requests", "prior_build"}
                            or type(manifest["requests"]) is not list or not manifest["requests"]):
                        raise UserError("清单需要包含 requests 和 prior_build。")
            else:
                request = new_request(body, self.config)
                run_id = request["operation_id"]
                directory = self.state / run_id
                directory.mkdir(mode=0o700)
                save(directory / "request.json", request)
                action, prior_entry = "start", None
                option = {"label": "处理新 Vault"}
            attempt_id = f"a-{time.time_ns():020d}-{uuid4().hex[:4]}"
            attempt = directory / "attempts" / attempt_id
            attempt.mkdir(mode=0o700, parents=True)
            if manifest is not None:
                save(attempt / "mapping-recovery.json", manifest)
            record = {"id": attempt_id, "action": action, "label": option["label"], "started_at": time.time()}
            if prior_entry:
                record["prior_entry"] = prior_entry
            path = attempt / "attempt.json"
            save(path, record)
            save(self.state / "active.json", {"run": run_id, "attempt": attempt_id})
            try:
                subprocess.Popen([sys.executable, "-B", str(HERE / "server.py"), "_attempt", str(directory),
                                  attempt_id, json.dumps(self.config), str(lease.fileno())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, pass_fds=(lease.fileno(),))
            except OSError as error:
                (attempt / "pipeline.log").write_text(str(error), encoding="utf-8")
                save(path, {**record, "returncode": 1, "ended_at": time.time()})
                raise UserError("无法启动本机执行器，请查看 DevTools。") from error
        return {"id": run_id, "attempt": attempt_id}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[HTTP] {format % args}", flush=True)

    def send(self, status, body, media):
        self.send_response(status)
        self.send_header("Content-Type", f"{media}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=HTTPStatus.OK):
        self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json")

    def validate_request(self, write):
        port = self.server.server_port
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        allowed = host in hosts and (origin is None or origin == f"http://{host}")
        allowed = allowed and self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}
        if not allowed:
            raise PermissionError("仅接受来自本机控制台的请求。")
        if write and self.headers.get_content_type() != "application/json":
            raise UserError("请求必须使用 JSON。")

    def do_GET(self):
        self.route(write=False)

    def do_POST(self):
        self.route(write=True)

    def route(self, write):
        try:
            self.validate_request(write)
            url = urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            app = self.server.console
            if not write and url.path in ASSETS:
                name, media = ASSETS[url.path]
                self.send(200, (HERE / "web" / name).read_bytes(), media)
            elif not write and url.path == "/api/runs":
                self.json({"runs": app.list_runs(), "writable": app.config["writable"]})
            elif write and url.path == "/api/runs":
                self.json(app.launch(self.payload()), HTTPStatus.ACCEPTED)
            elif parts[:2] == ["api", "runs"] and len(parts) in {3, 4}:
                run_id = parts[2]
                directory, historical = app.find(run_id)
                if not write and len(parts) == 3:
                    self.json(progress(directory, historical=historical, active=app.is_active(directory, historical)))
                elif write and parts[3:] == ["reenter"]:
                    self.json(app.launch(self.payload(), run_id), HTTPStatus.ACCEPTED)
                elif not write and parts[3:] == ["log"]:
                    entries = attempts_for(directory, historical)
                    default = entries[-1]["id"] if entries else None
                    selected = parse_qs(url.query).get("attempt", [default])[0]
                    entry = next((item for item in entries if item["id"] == selected), None)
                    if entry is None:
                        raise FileNotFoundError("attempt")
                    if historical:
                        log = directory / entry["log"]
                    else:
                        log = directory / "attempts" / selected / "pipeline.log"
                    self.json({"text": log_tail(log)})
                else:
                    raise FileNotFoundError(url.path)
            else:
                raise FileNotFoundError(url.path)
        except (UserError, UnicodeError, json.JSONDecodeError) as error:
            self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except PermissionError as error:
            self.json({"error": str(error)}, HTTPStatus.FORBIDDEN)
        except FileNotFoundError:
            self.json({"error": "找不到运行或资源。"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("%s: %s", type(error).__name__, error)
            self.json({"error": "无法读取本机运行状态，请检查控制台日志。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def payload(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or not 0 < int(length) <= 65536:
            raise UserError("请求为空或超出大小限制。")
        body = json.loads(self.rfile.read(int(length)))
        if not isinstance(body, dict):
            raise UserError("请求必须是 JSON 对象。")
        return body


def main():
    if sys.argv[1:2] == ["_attempt"]:
        _, directory, attempt_id, config, lease = sys.argv[1:]
        with os.fdopen(int(lease), "a") as lock:
            run_attempt(Path(directory), attempt_id, json.loads(config), lock.fileno())
        return
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    parser.add_argument("--port", type=int, default=18465)
    parser.add_argument("--state", type=Path, default=home / "operator-console")
    parser.add_argument("--skills-root", type=Path, default=home / "skills/domain")
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--provider-state", type=Path, default=Path.home() / ".local/state/qmd-like-rag")
    parser.add_argument("--wsl-distro", default=os.environ.get("WSL_DISTRO_NAME", "Ubuntu-24.04"))
    parser.add_argument("--history", type=Path, action="append", default=[])
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    skills = args.skills_root.resolve()
    config = {"state": str(args.state.resolve()), "skills_root": str(skills), "distro": args.wsl_distro,
              "provider_config": str((args.provider_config or skills / FINALIZE / "config/retrieval-provider.json").resolve()),
              "provider_state": str(args.provider_state.resolve()),
              "writable": platform.system() == "Linux" and not args.read_only}
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.console = Console(config, [p.resolve() for p in args.history])
        print(f"Hermes Console · http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
