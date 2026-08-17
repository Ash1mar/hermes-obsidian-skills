#!/usr/bin/env python3
"""Run optional coarse recall and hierarchical location in parallel, then fuse their scope."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


RRF_K = 60
DEFAULT_COMPACT_LIMIT = 5


def run_json(command: list[str]) -> dict[str, Any]:
    started = time.monotonic_ns()
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    duration_ms = round((time.monotonic_ns() - started) / 1_000_000, 3)
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("command output is not a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        result = {
            "status": "unavailable",
            "authority": "candidate-navigation-only",
            "candidates": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
        }
    result["duration_ms"] = duration_ms
    if completed.returncode not in {0, 2}:
        result["status"] = "unavailable"
        result.setdefault("warnings", []).append(
            f"command-exit-{completed.returncode}:{completed.stderr.strip()}"
        )
    return result


def normalized_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def ranges_for(section: dict[str, Any]) -> list[dict[str, int]]:
    ranges = []
    for item in section.get("content_ranges", []):
        try:
            start, end = int(item.get("start_line") or 1), int(item.get("end_line") or 1)
        except (AttributeError, TypeError, ValueError):
            continue
        if start >= 1 and end >= start:
            ranges.append({"start_line": start, "end_line": end})
    if not ranges:
        try:
            start = int(section.get("start_line") or 1)
            end = int(section.get("end_line") or start)
        except (TypeError, ValueError):
            return []
        if start >= 1 and end >= start:
            ranges.append({"start_line": start, "end_line": end})
    return ranges


def overlaps(start: int, end: int, ranges: list[dict[str, int]]) -> bool:
    return any(start <= item["end_line"] and end >= item["start_line"] for item in ranges)


def load_projections(vault_root: Path) -> dict[str, dict[str, Any]]:
    projections: dict[str, dict[str, Any]] = {}
    index_root = vault_root / "_system" / "reports" / "query-index"
    for path in index_root.glob("*.section-query-index.json"):
        try:
            projection = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        document = projection.get("document", {})
        document_path = normalized_path(document.get("document_path"))
        if document_path:
            projections[document_path] = projection
    return projections


def best_section(projection: dict[str, Any], start: int, end: int) -> dict[str, Any] | None:
    matches = []
    for section in projection.get("sections", []):
        owned = ranges_for(section)
        if not overlaps(start, end, owned):
            continue
        span = sum(item["end_line"] - item["start_line"] + 1 for item in owned)
        depth = len(section.get("path_titles", []))
        matches.append((span, -depth, str(section.get("section_id") or ""), section))
    return min(matches, key=lambda item: item[:3])[3] if matches else None


def canonical_hierarchical(item: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "document_path": normalized_path(item.get("document_path")),
        "source_filename": item.get("source_filename"),
        "section_id": item.get("section_id"),
        "title": item.get("title"),
        "path_titles": item.get("path_titles", []),
        "start_line": item.get("start_line"),
        "end_line": item.get("end_line"),
        "content_ranges": ranges_for(item),
        "pages": item.get("pages", []),
        "assets": item.get("assets", []),
        "quality": item.get("quality"),
        "ingest_status": item.get("ingest_status"),
        "viewer_url": item.get("viewer_url"),
        "matched_terms": item.get("matched_terms", {}),
        "retrieval_routes": ["hierarchical-search"],
        "route_ranks": {"hierarchical-search": rank},
        "route_scores": {"hierarchical-search": item.get("score")},
    }


def canonical_coarse(
    item: dict[str, Any], rank: int, projections: dict[str, dict[str, Any]], provider: str
) -> dict[str, Any]:
    document_path = normalized_path(item.get("vault_path"))
    start, end = int(item.get("line_start") or 1), int(item.get("line_end") or 1)
    projection = projections.get(document_path)
    section = best_section(projection, start, end) if projection else None
    route = str(provider or "coarse-recall")
    if section:
        document = projection.get("document", {})
        candidate = {
            "document_path": document_path,
            "source_filename": document.get("source_filename"),
            "section_id": section.get("section_id"),
            "title": section.get("title") or item.get("heading"),
            "path_titles": section.get("path_titles", []),
            "start_line": section.get("start_line"),
            "end_line": section.get("end_line"),
            "content_ranges": ranges_for(section),
            "pages": section.get("pages", []),
            "assets": section.get("assets", []),
            "quality": section.get("quality"),
            "ingest_status": section.get("ingest_status"),
        }
    else:
        candidate = {
            "document_path": document_path,
            "source_filename": None,
            "section_id": None,
            "title": item.get("heading") or Path(document_path).name,
            "path_titles": [item.get("heading")] if item.get("heading") else [],
            "start_line": start,
            "end_line": end,
            "content_ranges": [{"start_line": start, "end_line": end}],
            "pages": [],
            "assets": [],
            "quality": None,
            "ingest_status": None,
        }
    candidate.update(
        {
            "matched_terms": {},
            "retrieval_routes": [route],
            "route_ranks": {route: rank},
            "route_scores": {route: item.get("score")},
            "provider_ref": item.get("provider_ref"),
            "source_hash_matches": item.get("source_hash_matches"),
        }
    )
    return candidate


def title_key(candidate: dict[str, Any]) -> str:
    return re.sub(r"\s+", "", str(candidate.get("title") or "").casefold())


def candidate_ranges(candidate: dict[str, Any]) -> list[dict[str, int]]:
    return ranges_for(candidate)


def same_scope(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
    if left["document_path"] != right["document_path"]:
        return False, ""
    left_id, right_id = left.get("section_id"), right.get("section_id")
    if left_id and right_id and left_id == right_id:
        return True, "duplicate-section-merged"
    same_title = title_key(left) and title_key(left) == title_key(right)
    left_ranges, right_ranges = candidate_ranges(left), candidate_ranges(right)
    range_overlap = any(
        overlaps(item["start_line"], item["end_line"], right_ranges) for item in left_ranges
    )
    if same_title and range_overlap:
        return True, "duplicate-title-range-merged"
    if not left_id and not right_id and range_overlap:
        return True, "duplicate-document-range-merged"
    return False, ""


def merge_candidate(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    target["retrieval_routes"] = sorted(
        set(target.get("retrieval_routes", [])) | set(incoming.get("retrieval_routes", []))
    )
    target.setdefault("route_ranks", {}).update(incoming.get("route_ranks", {}))
    target.setdefault("route_scores", {}).update(incoming.get("route_scores", {}))
    for key in (
        "source_filename",
        "section_id",
        "title",
        "quality",
        "ingest_status",
        "viewer_url",
    ):
        if not target.get(key) and incoming.get(key):
            target[key] = incoming[key]
    for key in ("pages", "assets", "path_titles"):
        target[key] = list(dict.fromkeys([*target.get(key, []), *incoming.get(key, [])]))
    target["content_ranges"] = sorted(
        {
            (item["start_line"], item["end_line"])
            for item in [*candidate_ranges(target), *candidate_ranges(incoming)]
        }
    )
    target["content_ranges"] = [
        {"start_line": start, "end_line": end} for start, end in target["content_ranges"]
    ]


def fuse_candidates(
    hierarchical: dict[str, Any], coarse: dict[str, Any], projections: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inputs = [
        canonical_hierarchical(item, rank)
        for rank, item in enumerate(hierarchical.get("candidates", []), start=1)
    ]
    provider = str(coarse.get("provider") or "coarse-recall")
    inputs.extend(
        canonical_coarse(item, rank, projections, provider)
        for rank, item in enumerate(coarse.get("candidates", []), start=1)
    )
    retained: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in inputs:
        duplicate = None
        reason = ""
        for existing in retained:
            matched, matched_reason = same_scope(existing, candidate)
            if matched:
                duplicate, reason = existing, matched_reason
                break
        if duplicate is None:
            retained.append(candidate)
            continue
        merge_candidate(duplicate, candidate)
        rejected.append(
            {
                "path": candidate["document_path"],
                "title": candidate.get("title"),
                "reason": reason,
                "merged_into": duplicate.get("section_id") or duplicate.get("title"),
                "route_ranks": candidate.get("route_ranks", {}),
                "route_scores": candidate.get("route_scores", {}),
            }
        )
    for candidate in retained:
        candidate["fusion_score"] = round(
            sum(1.0 / (RRF_K + int(rank)) for rank in candidate.get("route_ranks", {}).values()),
            8,
        )
        candidate["rerank_score"] = candidate["fusion_score"]
    retained.sort(
        key=lambda item: (
            -float(item["fusion_score"]),
            min(item.get("route_ranks", {}).values(), default=10**9),
            item["document_path"],
            str(item.get("section_id") or ""),
        )
    )
    return retained, rejected


def compact_candidate(candidate: dict[str, Any], rank: int | None = None) -> dict[str, Any]:
    matched = candidate.get("matched_terms") or {}
    terms = sorted(
        {term for values in matched.values() if isinstance(values, list) for term in values},
        key=lambda value: (-len(value), value),
    )[:8]
    result = {
        "rank": rank,
        "document_path": candidate.get("document_path") or candidate.get("vault_path"),
        "source_filename": candidate.get("source_filename"),
        "section_id": candidate.get("section_id"),
        "title": candidate.get("title") or candidate.get("heading"),
        "pages": candidate.get("pages", []),
        "quality": candidate.get("quality"),
        "ingest_status": candidate.get("ingest_status"),
        "viewer_url": candidate.get("viewer_url"),
        "retrieval_routes": candidate.get("retrieval_routes", []),
        "fusion_score": candidate.get("fusion_score"),
        "matched_terms": terms,
    }
    return {key: value for key, value in result.items() if value not in (None, [], {})}


def compact_result(result: dict[str, Any], limit: int = DEFAULT_COMPACT_LIMIT) -> dict[str, Any]:
    candidates = result.get("candidates", [])
    return {
        "status": result.get("status"),
        "authority": result.get("authority"),
        "query": result.get("query"),
        "duration_ms": result.get("duration_ms"),
        "routes": result.get("routes", {}),
        "fusion": result.get("fusion", {}),
        "candidate_count": len(candidates),
        "candidates": [
            compact_candidate(candidate, rank)
            for rank, candidate in enumerate(candidates[: max(1, limit)], start=1)
        ],
        "warnings": result.get("warnings", []),
        "next_step": result.get("next_step"),
    }


def append_trace_events(
    vault_root: Path,
    trace_id: str,
    coarse: dict[str, Any],
    hierarchical: dict[str, Any],
    fused: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    fusion_duration_ms: float,
) -> None:
    try:
        from manage_query_trace import append_events

        append_events(
            vault_root,
            trace_id,
            [
                {
                "stage": "coarse-recall",
                "route": str(coarse.get("provider") or "qmd-like-rag"),
                "status": coarse.get("status"),
                "summary": "Optional coarse recall completed; unavailable Provider does not block the query.",
                "hit_count": len(coarse.get("candidates", [])),
                "duration_ms": coarse.get("duration_ms"),
                "accounting": "diagnostic",
                "candidates": coarse.get("candidates", [])[:3],
                },
                {
                "stage": "hierarchical-candidate-location",
                "route": "hierarchical-search",
                "status": hierarchical.get("status"),
                "summary": "Hierarchical document/section candidates located for scope fusion.",
                "hit_count": len(hierarchical.get("candidates", [])),
                "duration_ms": hierarchical.get("duration_ms"),
                "accounting": "diagnostic",
                "candidates": hierarchical.get("candidates", [])[:3],
                },
                {
                "stage": "candidate-fusion",
                "route": "candidate-fusion",
                "status": "ok" if fused else "empty",
                "summary": f"Normalized and fused candidates; retained {len(fused)}, merged {len(rejected)} duplicates.",
                "hit_count": len(fused),
                "duration_ms": fusion_duration_ms,
                "accounting": "diagnostic",
                "rejected": rejected,
                "candidates": fused,
                },
            ],
        )
    except Exception as exc:
        print(f"warning: query trace append failed: {exc}", file=sys.stderr)


def retrieve_scope(
    vault_root: Path,
    query: str,
    *,
    top_k: int = 30,
    top_documents: int = 8,
    top_sections: int = 20,
    provider_config: Path | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    script_root = Path(__file__).resolve().parent
    coarse_command = [
        sys.executable,
        str(script_root / "retrieve_candidates.py"),
        str(vault_root),
        query,
        "--top-k",
        str(top_k),
    ]
    if provider_config:
        coarse_command.extend(["--provider-config", str(provider_config)])
    hierarchical_command = [
        sys.executable,
        str(script_root / "locate_source_sections.py"),
        str(vault_root),
        query,
        "--top-documents",
        str(top_documents),
        "--top-sections",
        str(top_sections),
    ]
    started = time.monotonic_ns()
    with ThreadPoolExecutor(max_workers=2) as executor:
        coarse_future = executor.submit(run_json, coarse_command)
        hierarchical_future = executor.submit(run_json, hierarchical_command)
        coarse, hierarchical = coarse_future.result(), hierarchical_future.result()
    fusion_started = time.monotonic_ns()
    projections = load_projections(vault_root)
    fused, rejected = fuse_candidates(hierarchical, coarse, projections)
    fusion_duration_ms = round((time.monotonic_ns() - fusion_started) / 1_000_000, 3)
    duration_ms = round((time.monotonic_ns() - started) / 1_000_000, 3)
    if trace_id:
        append_trace_events(
            vault_root,
            trace_id,
            coarse,
            hierarchical,
            fused,
            rejected,
            fusion_duration_ms,
        )
    warnings = [*coarse.get("warnings", []), *hierarchical.get("errors", [])]
    result = {
        "status": "ok" if fused else "warn",
        "authority": "candidate-navigation-only",
        "query": query,
        "duration_ms": duration_ms,
        "routes": {
            "coarse_recall": {
                "status": coarse.get("status"),
                "provider": coarse.get("provider"),
                "duration_ms": coarse.get("duration_ms"),
                "hit_count": len(coarse.get("candidates", [])),
            },
            "hierarchical_search": {
                "status": hierarchical.get("status"),
                "duration_ms": hierarchical.get("duration_ms"),
                "hit_count": len(hierarchical.get("candidates", [])),
            },
        },
        "fusion": {
            "duration_ms": fusion_duration_ms,
            "retained_count": len(fused),
            "eliminated_count": len(rejected),
        },
        "candidates": fused,
        "rejected": rejected,
        "warnings": warnings,
        "next_step": "Inspect governed candidates first; when insufficient, run exact lexical search within this fused scope, then verify current source/PDF evidence.",
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--top-documents", type=int, default=8)
    parser.add_argument("--top-sections", type=int, default=20)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--trace-id")
    parser.add_argument("--compact", action="store_true", help="Print a bounded agent-facing result")
    parser.add_argument("--compact-limit", type=int, default=DEFAULT_COMPACT_LIMIT)
    parser.add_argument("--full-result-path", type=Path, help="Also save the complete JSON result")
    args = parser.parse_args()
    result = retrieve_scope(
        args.vault_root,
        args.query,
        top_k=args.top_k,
        top_documents=args.top_documents,
        top_sections=args.top_sections,
        provider_config=args.provider_config,
        trace_id=args.trace_id,
    )
    if args.full_result_path:
        args.full_result_path.parent.mkdir(parents=True, exist_ok=True)
        args.full_result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    output = compact_result(result, args.compact_limit) if args.compact else result
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
