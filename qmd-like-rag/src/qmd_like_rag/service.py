from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import ProviderConfig
from . import __version__
from .runtime import doctor, read_status, recall, sync


def make_handler(config: ProviderConfig) -> type[BaseHTTPRequestHandler]:
    runtime_lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"qmd-like-rag/{__version__}"

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ValueError("Request body exceeds 1 MiB")
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            return data

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._write(200, doctor())
            elif self.path == "/status":
                with runtime_lock:
                    self._write(200, read_status(config))
            else:
                self._write(404, {"status": "error", "error": "not-found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._payload()
                if self.path == "/retrieve":
                    query = str(payload.get("query") or "").strip()
                    if not query:
                        raise ValueError("query is required")
                    with runtime_lock:
                        result = recall(config, query, int(payload.get("top_k") or config.rerank_top_k))
                    self._write(200, result)
                elif self.path == "/sync":
                    with runtime_lock:
                        result = sync(config, bool(payload.get("rebuild", False)))
                    self._write(200, result)
                else:
                    self._write(404, {"status": "error", "error": "not-found"})
            except Exception as exc:
                self._write(400, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve(config: ProviderConfig, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.serve_forever()
