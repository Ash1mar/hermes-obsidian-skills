#!/usr/bin/env python3
"""Synchronize the configured coarse-recall Provider and write a portable Vault manifest."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request


PROTOCOL_VERSION = "hermes-coarse-recall/v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def call_command(config: dict[str, Any], vault_root: Path, rebuild: bool) -> dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("command transport requires a non-empty string array")
    args = [*command, "sync", "--vault-root", str(vault_root)]
    if rebuild:
        args.append("--rebuild")
    for option, flag in (("provider_config", "--config"), ("state_root", "--state-root"), ("vault_id", "--vault-id")):
        if config.get(option):
            args.extend([flag, str(config[option])])
    completed = subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=int(config.get("timeout_seconds", 1800)),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Provider command failed ({completed.returncode}): {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Provider command did not return one JSON object on stdout") from exc


def call_http(config: dict[str, Any], rebuild: bool) -> dict[str, Any]:
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError("http transport requires base_url")
    req = request.Request(
        base_url + "/sync",
        data=json.dumps({"rebuild": rebuild}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=int(config.get("timeout_seconds", 1800))) as response:
        return json.loads(response.read().decode("utf-8"))


def portable_manifest(payload: dict[str, Any], transport: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    now = utc_now()
    ready = payload.get("status") == "ready" and payload.get("protocol_version") == PROTOCOL_VERSION
    manifest = {
        "schema_version": "1.0",
        "authority": "retrieval-index-status",
        "provider": payload.get("provider", "qmd-like-rag"),
        "provider_version": payload.get("provider_version"),
        "protocol_version": payload.get("protocol_version"),
        "transport": transport,
        "vault_id": payload.get("vault_id"),
        "status": payload.get("status", "failed"),
        "last_attempt": now,
        "last_success": now if ready else (previous or {}).get("last_success"),
        "configuration_fingerprint": payload.get("configuration_fingerprint"),
        "model_fingerprint": payload.get("model_fingerprint"),
        "corpus_fingerprint": payload.get("corpus_fingerprint"),
        "index_fingerprint": payload.get("index_fingerprint"),
        "document_count": payload.get("document_count", 0),
        "chunk_count": payload.get("chunk_count", 0),
        "errors": payload.get("errors", []),
    }
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--no-write-manifest", action="store_true")
    args = parser.parse_args()
    vault_root = args.vault_root.resolve()
    manifest_path = vault_root / "_system" / "reports" / "retrieval-index-manifest.json"
    previous = load_json(manifest_path) if manifest_path.is_file() else None
    try:
        _, config = provider_config(args.provider_config)
        if config.get("enabled", True) is False:
            transport = "disabled"
            payload = {
                "provider": str(config.get("provider") or "qmd-like-rag"),
                "protocol_version": PROTOCOL_VERSION,
                "status": "disabled",
                "errors": [],
            }
        else:
            transport = str(config.get("transport") or "command")
            if transport == "command":
                payload = call_command(config, vault_root, args.rebuild)
            elif transport == "http":
                payload = call_http(config, args.rebuild)
            else:
                raise ValueError(f"Unsupported Provider transport: {transport}")
    except Exception as exc:
        transport = "unavailable"
        payload = {
            "provider": "qmd-like-rag",
            "protocol_version": PROTOCOL_VERSION,
            "status": "failed",
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    manifest = portable_manifest(payload, transport, previous)
    if not args.no_write_manifest:
        write_manifest(manifest_path, manifest)
    print(
        json.dumps(
            {
                "status": "ok" if manifest["status"] == "ready" else "warn",
                "manifest_path": manifest_path.as_posix() if not args.no_write_manifest else None,
                "index": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if manifest["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
