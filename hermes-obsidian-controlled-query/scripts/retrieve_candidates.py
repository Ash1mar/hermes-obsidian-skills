#!/usr/bin/env python3
"""Call the configured coarse-recall Provider and return validated navigation candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import request


PROTOCOL_VERSION = "hermes-coarse-recall/v1"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def provider_config(path: Path | None) -> tuple[Path, dict[str, Any]]:
    configured = path or (
        Path(os.environ["HERMES_RETRIEVAL_PROVIDER_CONFIG"])
        if os.environ.get("HERMES_RETRIEVAL_PROVIDER_CONFIG")
        else Path(__file__).resolve().parents[1] / "config" / "retrieval-provider.json"
    )
    return configured, load_json(configured)


def call_command(config: dict[str, Any], vault_root: Path, query: str, top_k: int) -> dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("command transport requires a non-empty string array")
    args = [*command, "recall", "--vault-root", str(vault_root), "--query", query, "--top-k", str(top_k)]
    for option, flag in (("provider_config", "--config"), ("state_root", "--state-root"), ("vault_id", "--vault-id")):
        if config.get(option):
            args.extend([flag, str(config[option])])
    completed = subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=int(config.get("timeout_seconds", 60)),
        check=False,
    )
    if completed.returncode not in {0, 2}:
        raise RuntimeError(f"Provider command failed ({completed.returncode}): {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Provider command did not return one JSON object on stdout") from exc


def call_http(config: dict[str, Any], query: str, top_k: int) -> dict[str, Any]:
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError("http transport requires base_url")
    body = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
    req = request.Request(
        base_url + "/retrieve",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=int(config.get("timeout_seconds", 60))) as response:
        return json.loads(response.read().decode("utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def validate_response(payload: dict[str, Any], vault_root: Path) -> dict[str, Any]:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported Provider protocol: {payload.get('protocol_version')!r}")
    if payload.get("authority") != "candidate-navigation-only":
        raise ValueError("Provider results must be navigation-only")
    normalized: list[dict[str, Any]] = []
    warnings = [str(item) for item in payload.get("warnings", [])]
    for candidate in payload.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        relative = Path(str(candidate.get("vault_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            warnings.append("candidate-path-rejected")
            continue
        source = (vault_root / relative).resolve()
        try:
            source.relative_to(vault_root.resolve())
        except ValueError:
            warnings.append("candidate-outside-vault")
            continue
        if not source.is_file():
            warnings.append(f"candidate-missing:{relative.as_posix()}")
            continue
        start = int(candidate.get("line_start") or 1)
        end = int(candidate.get("line_end") or start)
        if start < 1 or end < start:
            warnings.append(f"candidate-lines-invalid:{relative.as_posix()}")
            continue
        item = dict(candidate)
        item["vault_path"] = relative.as_posix()
        item["line_start"] = start
        item["line_end"] = end
        item["retrieval_routes"] = [str(payload.get("provider") or "coarse-recall")]
        expected_hash = str(item.get("source_sha256") or "")
        item["source_hash_matches"] = not expected_hash or sha256(source) == expected_hash
        if not item["source_hash_matches"]:
            warnings.append(f"candidate-source-changed:{relative.as_posix()}")
        normalized.append(item)
    return {
        "status": "warn" if warnings or payload.get("status") == "warn" else "ok",
        "authority": "candidate-navigation-only",
        "provider": payload.get("provider"),
        "provider_version": payload.get("provider_version"),
        "index_fingerprint": payload.get("index_fingerprint"),
        "candidates": normalized,
        "warnings": sorted(set(warnings)),
        "next_step": "Fuse with hierarchical candidates, inspect governed candidates first, then run supplemental scoped exact/lexical search and verify current source/PDF evidence.",
    }


def append_trace(vault_root: Path, trace_id: str, result: dict[str, Any]) -> None:
    try:
        from manage_query_trace import append_event

        append_event(
            vault_root,
            trace_id,
            {
                "stage": "coarse-recall",
                "route": "qmd-like-rag",
                "status": result["status"],
                "summary": "Located coarse-recall candidates; results remain navigation-only until current-source verification.",
                "hit_count": len(result["candidates"]),
                "duration_ms": result.get("duration_ms"),
                "inspected_paths": sorted({item["vault_path"] for item in result["candidates"]}),
                "candidates": result["candidates"],
            },
        )
    except Exception as exc:
        print(f"warning: query trace append failed: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--trace-id")
    args = parser.parse_args()
    vault_root = args.vault_root.resolve()
    started = time.monotonic_ns()
    try:
        config_path, config = provider_config(args.provider_config)
        if config.get("enabled", True) is False:
            result = {
                "status": "disabled",
                "authority": "candidate-navigation-only",
                "provider": str(config.get("provider") or "qmd-like-rag"),
                "provider_config": config_path.as_posix(),
                "candidates": [],
                "warnings": [],
                "next_step": "Continue with hierarchical and traditional retrieval; the coarse-recall Provider is explicitly disabled.",
            }
        else:
            transport = str(config.get("transport") or "command")
            if transport == "command":
                payload = call_command(config, vault_root, args.query, args.top_k)
            elif transport == "http":
                payload = call_http(config, args.query, args.top_k)
            else:
                raise ValueError(f"Unsupported Provider transport: {transport}")
            result = validate_response(payload, vault_root)
            result["transport"] = transport
            result["provider_config"] = config_path.as_posix()
    except Exception as exc:
        result = {
            "status": "unavailable",
            "authority": "candidate-navigation-only",
            "provider": "qmd-like-rag",
            "candidates": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
            "next_step": "Continue with hierarchical and traditional retrieval; do not mutate or rebuild the index during query.",
        }
    result["duration_ms"] = round((time.monotonic_ns() - started) / 1_000_000, 3)
    if args.trace_id:
        append_trace(vault_root, args.trace_id, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
