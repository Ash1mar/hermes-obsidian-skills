from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__


PROTOCOL_VERSION = "hermes-coarse-recall/v1"
PROVIDER_ID = "qmd-like-rag"


def validate_relative_vault_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized in {"", "."}:
        raise ValueError(f"Invalid Vault-relative path: {value!r}")
    return path.as_posix()


def normalize_candidate(item: dict[str, Any], vault_root: Path) -> dict[str, Any]:
    source = str(item.get("vault_path") or item.get("source") or "")
    source_path = Path(source)
    if source_path.is_absolute():
        try:
            source = source_path.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"Provider candidate is outside the Vault: {source}") from exc
    source = validate_relative_vault_path(source)
    start = int(item.get("line_start") or item.get("start_line") or 1)
    end = int(item.get("line_end") or item.get("end_line") or start)
    if start < 1 or end < start:
        raise ValueError(f"Invalid line range for {source}: {start}-{end}")
    return {
        "vault_path": source,
        "line_start": start,
        "line_end": end,
        "heading": str(item.get("heading") or ""),
        "snippet": str(item.get("snippet") or item.get("text") or ""),
        "context": "",
        "score": float(item.get("rerank_score", item.get("rrf_score", item.get("score", 0.0)))),
        "score_type": str(item.get("score_type") or "hybrid"),
        "provider_ref": str(item.get("provider_ref") or item.get("id") or ""),
        "source_sha256": str(item.get("source_sha256") or ""),
        "projection_kind": str(item.get("projection_kind") or ""),
        "projection_fingerprint": str(item.get("projection_fingerprint") or ""),
        "release_id": str(item.get("release_id") or ""),
        "release_hash": str(item.get("release_hash") or ""),
        "unit_ref": item.get("unit_ref"),
        "page_id": item.get("page_id"),
        "page_revision_id": item.get("page_revision_id"),
        "source_unit_refs": list(item.get("source_unit_refs") or []),
        "subspan": item.get("subspan"),
        "subspan_reason": item.get("subspan_reason"),
        "projection_content_sha256": str(item.get("projection_content_sha256") or ""),
    }


def recall_response(
    *,
    vault_id: str,
    index_fingerprint: str | None,
    release_id: str | None = None,
    release_hash: str | None = None,
    index_generation: str | None = None,
    candidates: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "provider": PROVIDER_ID,
        "provider_version": __version__,
        "status": "warn" if warnings else "ok",
        "authority": "candidate-navigation-only",
        "vault_id": vault_id,
        "index_fingerprint": index_fingerprint,
        "release_id": release_id,
        "release_hash": release_hash,
        "index_generation": index_generation,
        "capabilities": {"source_units": True, "release_driven": True,
                         "projection_kinds": ["source_unit", "knowledge_page"]},
        "candidates": candidates,
        "warnings": warnings or [],
        "next_step": "Fuse with hierarchical scope, run scoped lexical search, then verify the current Vault source and original-PDF evidence.",
    }
