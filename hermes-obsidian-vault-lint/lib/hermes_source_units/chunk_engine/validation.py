"""Candidate coverage and quality validation."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .models import CanonicalChunkSpan, ChunkProfile


def validate_candidate(text: str, sections: list[dict[str, Any]], spans: list[CanonicalChunkSpan],
                       profile: ChunkProfile, diagnostics: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    remaining = [(item.section_id, item.start, item.end) for item in spans]
    covered = 0
    coverage_complete = True
    for section in sections:
        for owned in section["owned_ranges"]:
            selected = sorted((start, end) for section_id, start, end in remaining
                              if section_id == section["section_id"] and start >= owned["start"] and end <= owned["end"])
            if not selected:
                reasons.append("owned range has no chunk"); coverage_complete = False; continue
            cursor = owned["start"]
            for start, end in selected:
                if start > cursor or end <= start:
                    reasons.append("chunk coverage has a gap or empty span"); coverage_complete = False; break
                cursor = max(cursor, end)
                remaining.remove((section["section_id"], start, end))
            if cursor != owned["end"]:
                reasons.append("chunks do not cover an owned range")
                coverage_complete = False
            covered += max(0, cursor - owned["start"])
    if remaining:
        reasons.append("chunk crosses or escapes its owned range")
        coverage_complete = False
    oversized = {(item["span"]["start"], item["span"]["end"])
                 for item in diagnostics if item.get("code") == "oversized-protected-structure"}
    if any(span.end - span.start > profile.max_codepoints and (span.start, span.end) not in oversized for span in spans):
        reasons.append("ordinary chunk exceeds max_codepoints")
    sizes = [span.end - span.start for span in spans]
    tiny_limit = max(1, profile.target_codepoints // 4)
    tiny = sum(size < tiny_limit for size in sizes)
    if len(sizes) >= 8 and tiny / len(sizes) > 0.8:
        reasons.append("excessive tiny chunk ratio")
    kinds = Counter(kind for span in spans for kind in span.structure_kinds)
    statistics = {
        "chunk_count": len(sizes), "minimum_codepoints": min(sizes, default=0),
        "maximum_codepoints": max(sizes, default=0),
        "average_codepoints": sum(sizes) // len(sizes) if sizes else 0,
        "tiny_chunk_count": tiny, "oversized_protected_count": len(oversized),
        "protected_kinds": [{"kind": kind, "count": count} for kind, count in sorted(kinds.items())],
        "covered_codepoints": covered,
        "document_codepoints": len(text), "coverage_complete": coverage_complete,
    }
    return list(dict.fromkeys(reasons)), statistics
