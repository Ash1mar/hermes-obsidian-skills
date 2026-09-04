from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import error, request


def post_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    max_retries: int,
    api_key_env: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key_env:
        token = os.environ.get(api_key_env)
        if not token:
            raise RuntimeError(f"Required model API key environment variable is unset: {api_key_env}")
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = request.Request(endpoint, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                raise RuntimeError("Model service response must be a JSON object")
            return decoded
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(0.25 * (2**attempt), 2.0))
    raise RuntimeError(f"Model service request failed after {max_retries + 1} attempt(s): {last_error}")
