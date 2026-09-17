from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def _fingerprint(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def render_projections(projections: Iterable[dict[str, Any]], tokenizer: Any,
                       renderer_version: str) -> list[dict[str, Any]]:
    rendered = []
    for projection in projections:
        heading = " / ".join(projection.get("heading_path", []))
        core = str(projection.get("core_text") or "")
        prefix = f"Context: {heading}\n\n" if heading else ""
        text = prefix + core
        tokens = tokenizer.count(text)
        protected = projection.get("oversized_protected_spans", [])
        locator_span = projection.get("locator_span")
        allow_subspans = (projection.get("projection_kind") == "source_unit"
                          and isinstance(locator_span, dict)
                          and any(span.get("start") <= locator_span.get("start")
                                  and span.get("end") >= locator_span.get("end")
                                  for span in protected if isinstance(span, dict)))
        if tokens > tokenizer.max_tokens and not allow_subspans:
            raise RuntimeError(
                f"Projection exceeds embedding limit ({tokens}>{tokenizer.max_tokens}): "
                f"{projection.get('unit_ref') or projection.get('page_revision_id')}"
            )
        spans = tokenizer.split(core, prefix) if tokens > tokenizer.max_tokens else [(0, len(core))]
        for relative_start, relative_end in spans:
            subspan = None
            if len(spans) > 1:
                subspan = {"start": int(locator_span["start"]) + relative_start,
                           "end": int(locator_span["start"]) + relative_end}
            rendered.append(_render_one(projection, tokenizer, renderer_version, heading, prefix,
                                        core[relative_start:relative_end], subspan))
    return rendered


def _render_one(projection: dict[str, Any], tokenizer: Any, renderer_version: str,
                heading: str, prefix: str, core: str,
                subspan: dict[str, int] | None) -> dict[str, Any]:
    text = prefix + core
    tokens = tokenizer.count(text)
    if tokens > tokenizer.max_tokens:
        raise RuntimeError("Tokenizer split produced an over-limit projection")
    identity = projection.get("unit_ref") or {
        "page_id": projection.get("page_id"), "page_revision_id": projection.get("page_revision_id")}
    projection_content_sha256 = hashlib.sha256(core.encode("utf-8")).hexdigest()
    subspan_reason = "oversized-protected-structure" if subspan is not None else None
    projection_fingerprint = _fingerprint({
        "renderer_version": renderer_version, "identity": identity,
        "content_sha256": projection["content_sha256"], "heading_path": projection.get("heading_path", []),
        "tokenizer_fingerprint": tokenizer.fingerprint,
            "subspan": subspan,
            "projection_content_sha256": projection_content_sha256,
    })
    return {
        "id": projection_fingerprint.removeprefix("sha256:"), "text": text, "core_text": core,
        "source": projection["vault_path"], "source_sha256": projection["source_sha256"],
        "start_line": projection["line_start"], "end_line": projection["line_end"],
        "heading": heading, "chunk_type": projection["projection_kind"],
        "projection_kind": projection["projection_kind"],
        "projection_fingerprint": projection_fingerprint, "renderer_version": renderer_version,
        "release_id": projection["release_id"], "release_hash": projection["release_hash"],
        "unit_ref": projection.get("unit_ref"), "page_id": projection.get("page_id"),
        "page_revision_id": projection.get("page_revision_id"),
        "source_unit_refs": projection.get("source_unit_refs", []), "token_count": tokens,
        "subspan": subspan,
        "subspan_reason": subspan_reason,
        "projection_content_sha256": projection_content_sha256,
    }
