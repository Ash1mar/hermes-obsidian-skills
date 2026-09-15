"""Canonical overlap constrained to one contiguous owned range."""
from __future__ import annotations

from typing import Any

from .models import CanonicalChunkSpan, ChunkProfile, TokenCounter


def apply_overlap(text: str, spans: list[CanonicalChunkSpan], profile: ChunkProfile,
                  protected: list[tuple[int, int]], token_counter: TokenCounter | None,
                  diagnostics: list[dict[str, Any]]) -> list[CanonicalChunkSpan]:
    if not profile.overlap_codepoints:
        return spans
    result: list[CanonicalChunkSpan] = []
    for span in spans:
        start = span.start
        previous = result[-1] if result else None
        if previous and previous.section_id == span.section_id and previous.end == span.start:
            candidate = max(previous.start, span.start - profile.overlap_codepoints)
            crosses_structure = any(lo < span.start and hi > candidate for lo, hi in protected)
            exceeds_codepoints = span.end - candidate > profile.max_codepoints
            exceeds_tokens = bool(token_counter and profile.token_mode == "hard"
                                  and token_counter.count(text[candidate:span.end]) > profile.max_tokens)
            if not (crosses_structure or exceeds_codepoints or exceeds_tokens):
                start = candidate
            else:
                diagnostics.append({"code": "overlap-reduced", "section_id": span.section_id,
                                    "at": span.start, "requested": profile.overlap_codepoints,
                                    "actual": span.start - start})
        result.append(CanonicalChunkSpan(span.section_id, start, span.end, span.structure_kinds))
    return result
