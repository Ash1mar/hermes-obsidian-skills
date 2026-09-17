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

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from hermes_source_units import FileSourceUnitService
from locate_source_sections import GovernanceQueryPolicy


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


def current_release(vault_root: Path) -> dict[str, Any]:
    state = load_json(vault_root / "_system" / "metadata" / "knowledge-release-state.json")
    release_id = state.get("current_release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("Vault has no current knowledge release")
    path = vault_root / "_system" / "knowledge-releases" / release_id / "manifest.json"
    data = path.read_bytes()
    release = json.loads(data.decode("utf-8"))
    if release.get("release_id") != release_id or release.get("state") != "completed":
        raise ValueError("Current release pointer and manifest disagree")
    return {"release_id": release_id, "release_hash": hashlib.sha256(data).hexdigest(),
            "manifest": release}


def registry_revision(vault_root: Path, reader: FileSourceUnitService) -> int:
    relative = str(reader.vault_manifest["governance"]["repository"]["registry_path"])
    value = load_json(vault_root / relative).get("registry_revision")
    if type(value) is not int:
        raise ValueError("Document registry revision is invalid")
    return value


def read_source_unit(candidate: dict[str, Any], reader: FileSourceUnitService,
                     revision: int) -> dict[str, Any]:
    ref = candidate.get("unit_ref")
    if not isinstance(ref, dict):
        raise ValueError("source_unit candidate has no complete UnitRef")
    subspan = candidate.get("subspan")
    if subspan is not None and (not isinstance(subspan, dict)
                                or type(subspan.get("start")) is not int
                                or type(subspan.get("end")) is not int):
        raise ValueError("source_unit candidate has an invalid subspan")
    if subspan is not None and candidate.get("subspan_reason") != "oversized-protected-structure":
        raise ValueError("source_unit subspan has no approved exception reason")
    expected_projection_hash = str(candidate.get("projection_content_sha256") or "")
    if (len(expected_projection_hash) != 64
            or any(value not in "0123456789abcdef" for value in expected_projection_hash)):
        raise ValueError("source_unit candidate has no valid projection content hash")
    result = reader.get({"source_ref": {"unit_ref": ref, "span": subspan},
                         "access": {"actor": "hermes-query", "purpose": "query",
                                    "registry_revision": revision}})
    selected = result.get("core_text")
    if (isinstance(selected, str)
            and hashlib.sha256(selected.encode("utf-8")).hexdigest() != expected_projection_hash):
        raise ValueError("source_unit projection content hash mismatch")
    item = dict(candidate)
    item["core_text"] = selected
    item["core_content_sha256"] = result["content_sha256"]
    item["asset_refs"] = result.get("asset_refs", [])
    item["quality_refs"] = result.get("quality_refs", [])
    item["exact_source_unit"] = True
    return item


def validate_response(
    payload: dict[str, Any],
    vault_root: Path,
    include_historical: bool = False,
) -> dict[str, Any]:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported Provider protocol: {payload.get('protocol_version')!r}")
    if payload.get("authority") != "candidate-navigation-only":
        raise ValueError("Provider results must be navigation-only")
    capabilities = payload.get("capabilities", {})
    if capabilities.get("source_units") is not True or capabilities.get("release_driven") is not True:
        raise ValueError("Provider lacks required SourceUnit release capability")
    release = current_release(vault_root)
    if payload.get("release_id") != release["release_id"] or payload.get("release_hash") != release["release_hash"]:
        raise ValueError("Provider index does not match the current knowledge release")
    if not payload.get("index_generation"):
        raise ValueError("Provider response has no index generation")
    eligible = {(row.get("kind"), row.get("id")): row
                for row in release["manifest"].get("index_eligibility", []) if row.get("eligible")}
    reader: FileSourceUnitService | None = None
    current_registry_revision: int | None = None
    normalized: list[dict[str, Any]] = []
    warnings = [str(item) for item in payload.get("warnings", [])]
    governance_policy = GovernanceQueryPolicy(vault_root, include_historical)
    governance_rejected = 0
    for candidate in payload.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        relative = Path(str(candidate.get("vault_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            warnings.append("candidate-path-rejected")
            continue
        projection_kind = str(candidate.get("projection_kind") or "")
        if projection_kind == "source_unit":
            ref = candidate.get("unit_ref")
            key = ("source_unit_set", ref.get("unit_set_id")) if isinstance(ref, dict) else (None, None)
            if key not in eligible:
                warnings.append("candidate-unit-ineligible")
                continue
            if reader is None:
                reader = FileSourceUnitService(vault_root)
                current_registry_revision = registry_revision(vault_root, reader)
        elif projection_kind == "knowledge_page":
            key = ("knowledge_page", candidate.get("page_id"))
            eligibility = eligible.get(key)
            if not eligibility or eligibility.get("page_revision_id") != candidate.get("page_revision_id"):
                warnings.append("candidate-page-ineligible")
                continue
        else:
            warnings.append("candidate-projection-kind-invalid")
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
        item = (read_source_unit(candidate, reader, int(current_registry_revision))
                if projection_kind == "source_unit" else dict(candidate))
        item["vault_path"] = relative.as_posix()
        item["line_start"] = start
        item["line_end"] = end
        item["retrieval_routes"] = [str(payload.get("provider") or "coarse-recall")]
        expected_hash = str(item.get("source_sha256") or "")
        item["source_hash_matches"] = (True if projection_kind == "source_unit"
                                       else not expected_hash or sha256(source) == expected_hash)
        if not item["source_hash_matches"]:
            warnings.append(f"candidate-source-changed:{relative.as_posix()}")
        if projection_kind == "knowledge_page" and not governance_policy.allows(item["vault_path"]):
            governance_rejected += 1
            continue
        normalized.append(item)
    if governance_rejected:
        warnings.append(f"governance-candidates-rejected:{governance_rejected}")
    return {
        "status": "warn" if warnings or payload.get("status") == "warn" else "ok",
        "authority": "candidate-navigation-only",
        "provider": payload.get("provider"),
        "provider_version": payload.get("provider_version"),
        "index_fingerprint": payload.get("index_fingerprint"),
        "release_id": payload.get("release_id"),
        "release_hash": payload.get("release_hash"),
        "index_generation": payload.get("index_generation"),
        "capabilities": capabilities,
        "candidates": normalized,
        "governance": governance_policy.summary(),
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
    parser.add_argument(
        "--include-historical",
        action="store_true",
        help="Allow completed superseded/withdrawn versions for an explicitly historical query",
    )
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
            result = validate_response(payload, vault_root, args.include_historical)
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
