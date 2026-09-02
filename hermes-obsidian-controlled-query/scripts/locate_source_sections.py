#!/usr/bin/env python3
"""Locate source-section candidates without treating the query projection as evidence.

Design origin: hanyu. This is a parallel candidate locator inside the existing
controlled-query source-navigation stage, not a second answer pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


DEFAULT_DEPLOYMENT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "deployment.json"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def load_deployment_config(explicit_path: Path | None = None) -> dict[str, Any]:
    env_path = os.environ.get("HERMES_DEPLOYMENT_CONFIG")
    configured_path = explicit_path or (Path(env_path).expanduser() if env_path else None)
    path = configured_path or DEFAULT_DEPLOYMENT_CONFIG
    if not path.is_file():
        if configured_path is not None:
            raise ValueError(f"Deployment config does not exist: {path}")
        return {}
    return load_json(path)


def load_viewer_base_url(
    explicit_url: str | None,
    deployment_config: dict[str, Any],
) -> str | None:
    value = explicit_url if explicit_url is not None else deployment_config.get("viewer_base_url")
    text = str(value or "").strip()
    return text.rstrip("?") or None


def query_terms(text: str) -> list[str]:
    terms: set[str] = set()
    for token in re.findall(
        r"[A-Za-z]+\d*[A-Za-z0-9._/-]*|\d+[A-Za-z][A-Za-z0-9._/-]*|\d+(?:\.\d+)?|[\u3400-\u9fff]+",
        text.casefold(),
    ):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            max_size = min(4, len(token))
            for size in range(2, max_size + 1):
                terms.update(token[index : index + size] for index in range(len(token) - size + 1))
        else:
            terms.add(token)
    return sorted(terms, key=lambda value: (-len(value), value))


def compact_terms(values: list[str], limit: int = 8) -> list[str]:
    """Keep the longest distinct matches so agent output is not flooded by n-grams."""
    retained: list[str] = []
    for value in sorted(set(values), key=lambda item: (-len(item), item)):
        if any(value in existing for existing in retained):
            continue
        retained.append(value)
        if len(retained) >= limit:
            break
    return retained


def match_score(terms: list[str], text: str, weight: int) -> tuple[int, list[str]]:
    folded = text.casefold()
    matched = [term for term in terms if term in folded]
    # Overlapping n-grams describe the same lexical signal and must not inflate
    # a long background section merely because it repeats the query subject.
    scored_matches = compact_terms(matched, limit=12)
    return sum(weight * max(1, len(term) - 1) for term in scored_matches), matched


def section_specific_query_terms(terms: list[str], document_matches: list[str]) -> list[str]:
    """Remove terms already satisfied by document identity when routing sections."""
    document_coverage = set(document_matches)
    residual = [term for term in terms if term not in document_coverage]
    return residual or terms


def read_ranges(lines: list[str], ranges: list[dict[str, Any]]) -> str:
    selected: list[str] = []
    for item in ranges:
        start = max(1, int(item.get("start_line") or 1))
        end = min(len(lines), int(item.get("end_line") or start))
        selected.extend(lines[start - 1 : end])
    return "\n".join(selected)


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def match_line_range(section: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return the complete matched range used for optional viewer highlighting."""
    try:
        start = int(section.get("start_line"))
        end = int(section.get("end_line"))
    except (TypeError, ValueError):
        starts: list[int] = []
        ends: list[int] = []
        for item in section.get("content_ranges", []):
            try:
                starts.append(int(item["start_line"]))
                ends.append(int(item["end_line"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not starts or not ends:
            return None, None
        start, end = min(starts), max(ends)
    if start < 1 or end < start:
        return None, None
    return start, end


def build_viewer_url(
    base_url: str | None,
    document_id: Any,
    section_id: Any,
    start_line: int | None,
    end_line: int | None,
) -> str | None:
    if not base_url or not document_id or not section_id or start_line is None or end_line is None:
        return None
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'doc': document_id, 'section': section_id, 'from': start_line, 'to': end_line})}"


def candidate_coverage_terms(candidate: dict[str, Any], *fields: str) -> set[str]:
    """Return compact matched query terms for coverage-aware candidate packing."""
    matched = candidate.get("matched_terms") or {}
    values = [
        str(value)
        for field in fields
        for value in matched.get(field, [])
        if str(value).strip()
    ]
    return set(compact_terms(values, limit=16))


def marginal_term_weight(terms: set[str], covered: set[str]) -> int:
    return sum(max(1, len(term)) for term in terms - covered)


def diversify_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Pack a fixed window for document diversity and complementary query coverage."""
    bounded_limit = max(1, limit)
    target_documents = min(3, bounded_limit)
    document_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        document_path = str(candidate.get("document_path") or "")
        if document_path not in grouped and len(document_order) < target_documents:
            document_order.append(document_path)
            grouped[document_path] = []
        if document_path in grouped:
            grouped[document_path].append(candidate)

    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    covered_structured: set[str] = set()
    covered_all: set[str] = set()

    def add(candidate: dict[str, Any]) -> None:
        document_path = str(candidate.get("document_path") or "")
        selected.append(candidate)
        selected_keys.add((document_path, str(candidate.get("section_id") or "")))
        covered_structured.update(candidate_coverage_terms(candidate, "title", "path"))
        covered_all.update(candidate_coverage_terms(candidate, "title", "path", "content", "document"))

    # Start with the strongest section, then give its document one early slot
    # for a section that adds distinct query-language coverage. This lets a
    # detailed section complement a calculation/overview section before broad
    # document diversity consumes the fixed window.
    if candidates:
        add(candidates[0])

    ranked_positions = {id(candidate): index for index, candidate in enumerate(candidates)}
    if selected:
        primary_document = str(selected[0].get("document_path") or "")
        primary_complements = [
            candidate
            for candidate in grouped.get(primary_document, [])[1:]
            if (str(candidate.get("document_path") or ""), str(candidate.get("section_id") or ""))
            not in selected_keys
        ]
        if primary_complements and len(selected) < bounded_limit:
            complement = max(
                primary_complements,
                key=lambda item: (
                    marginal_term_weight(
                        candidate_coverage_terms(item, "title", "path"), covered_structured
                    ),
                    marginal_term_weight(
                        candidate_coverage_terms(item, "title", "path", "content", "document"),
                        covered_all,
                    ),
                    -ranked_positions[id(item)],
                ),
            )
            structured_gain = marginal_term_weight(
                candidate_coverage_terms(complement, "title", "path"), covered_structured
            )
            all_gain = marginal_term_weight(
                candidate_coverage_terms(complement, "title", "path", "content", "document"),
                covered_all,
            )
            if structured_gain > 0 or all_gain > 0:
                add(complement)

    # Preserve the strongest section from the remaining documents after the
    # primary complementary slot has been considered.
    for document_path in document_order:
        if len(selected) >= bounded_limit:
            break
        candidate = grouped[document_path][0]
        key = (document_path, str(candidate.get("section_id") or ""))
        if key not in selected_keys:
            add(candidate)

    eligible = [
        candidate
        for document_path in document_order
        for candidate in grouped[document_path][1:]
        if (document_path, str(candidate.get("section_id") or "")) not in selected_keys
    ]
    while len(selected) < bounded_limit and eligible:
        candidate = max(
            eligible,
            key=lambda item: (
                marginal_term_weight(
                    candidate_coverage_terms(item, "title", "path"), covered_structured
                ),
                marginal_term_weight(
                    candidate_coverage_terms(item, "title", "path", "content", "document"),
                    covered_all,
                ),
                -ranked_positions[id(item)],
            ),
        )
        eligible.remove(candidate)
        add(candidate)

    if len(selected) >= bounded_limit:
        return selected

    for candidate in candidates:
        key = (str(candidate.get("document_path") or ""), str(candidate.get("section_id") or ""))
        if key in selected_keys:
            continue
        add(candidate)
        if len(selected) >= bounded_limit:
            break
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("query")
    parser.add_argument("--top-documents", type=int, default=8)
    parser.add_argument("--top-sections", type=int, default=20)
    parser.add_argument("--index-dir", type=Path, help="Defaults to <vault>/_system/reports/query-index")
    parser.add_argument("--no-content-scan", action="store_true", help="Score only document routing and section paths")
    parser.add_argument("--trace-id", help="Append actual candidates to an active query trace")
    parser.add_argument("--viewer-base-url", help="Override the optional deployment-local source viewer URL")
    parser.add_argument(
        "--deployment-config",
        type=Path,
        help=(
            "Optional deployment defaults. Falls back to HERMES_DEPLOYMENT_CONFIG, then "
            "config/deployment.json when present."
        ),
    )
    args = parser.parse_args()
    started = time.monotonic_ns()

    vault_root = args.vault_root.resolve()
    index_dir = (args.index_dir or vault_root / "_system" / "reports" / "query-index").resolve()
    try:
        deployment_config = load_deployment_config(args.deployment_config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    viewer_base_url = load_viewer_base_url(args.viewer_base_url, deployment_config)
    terms = query_terms(args.query)
    documents: list[tuple[int, Path, dict[str, Any], list[str]]] = []
    errors: list[str] = []
    for path in index_dir.glob("*.section-query-index.json"):
        try:
            projection = load_json(path)
            document = projection.get("document", {})
            routing = " ".join(
                [str(document.get("source_filename", "")), str(document.get("bundle_path", ""))]
                + [str(item) for item in document.get("routing_terms", [])]
            )
            score, matched = match_score(terms, routing, 5)
            documents.append((score, path, projection, matched))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")

    documents.sort(key=lambda item: (-item[0], item[1].name))
    candidates: list[dict[str, Any]] = []
    for document_score, index_path, projection, document_matches in documents[: max(1, args.top_documents)]:
        document = projection.get("document", {})
        section_terms = section_specific_query_terms(terms, document_matches)
        source_document = vault_root / str(document.get("document_path", ""))
        document_lines: list[str] | None = None
        if not args.no_content_scan and source_document.is_file():
            try:
                # A projection can contain hundreds of sections. Reading the source
                # once per document avoids multiplying WSL/NTFS boundary latency by
                # the section count.
                document_lines = source_document.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                errors.append(f"{source_document}: {exc}")
        for section in projection.get("sections", []):
            match_start_line, match_end_line = match_line_range(section)
            title_score, title_matches = match_score(section_terms, str(section.get("title", "")), 9)
            path_score, path_matches = match_score(
                section_terms, " / ".join(section.get("path_titles", [])), 4
            )
            content_score = 0
            content_matches: list[str] = []
            if document_lines is not None:
                content = read_ranges(document_lines, section.get("content_ranges", []))
                content_score, content_matches = match_score(section_terms, content, 2)
            section_score = title_score + path_score + content_score
            if section_score <= 0:
                continue
            score = min(document_score, 25) + section_score
            if score <= 0:
                continue
            status = str(section.get("ingest_status", "untracked"))
            if status in {"stale", "pending", "in_progress"}:
                score -= 3
            candidates.append(
                {
                    "score": score,
                    "document_score": document_score,
                    "document_id": document.get("document_id"),
                    "source_filename": document.get("source_filename"),
                    "document_path": document.get("document_path"),
                    "index_path": display_path(index_path, vault_root),
                    "section_id": section.get("section_id"),
                    "title": section.get("title"),
                    "path_titles": section.get("path_titles", []),
                    "start_line": section.get("start_line"),
                    "end_line": section.get("end_line"),
                    "match_start_line": match_start_line,
                    "match_end_line": match_end_line,
                    "viewer_url": build_viewer_url(
                        viewer_base_url,
                        document.get("document_id"),
                        section.get("section_id"),
                        match_start_line,
                        match_end_line,
                    ),
                    "content_ranges": section.get("content_ranges", []),
                    "pages": section.get("pages", []),
                    "assets": section.get("assets", []),
                    "quality": section.get("quality"),
                    "ingest_status": status,
                    "matched_terms": {
                        "document": compact_terms(document_matches),
                        "title": compact_terms(title_matches),
                        "path": compact_terms(path_matches),
                        "content": compact_terms(content_matches),
                    },
                }
            )

    candidates.sort(key=lambda item: (-int(item["score"]), str(item["source_filename"]), str(item["section_id"])))
    selected_candidates = diversify_candidates(candidates, args.top_sections)
    eligible_viewer_urls = list(
        dict.fromkeys(str(item["viewer_url"]) for item in selected_candidates if item.get("viewer_url"))
    )
    result = {
        "status": "warn" if errors else "ok",
        "authority": "candidate-navigation-only",
        "design_origin": "hanyu",
        "query": args.query,
        "terms": compact_terms(terms, limit=12),
        "candidates": selected_candidates,
        "ranking": {
            "strategy": "section-specific-score-with-document-and-query-coverage",
            "document_count": len({str(item.get("document_path") or "") for item in selected_candidates}),
            "matched_query_term_count": len(
                {
                    term
                    for item in selected_candidates
                    for term in candidate_coverage_terms(item, "title", "path", "content", "document")
                }
            ),
        },
        "answer_contract": {
            "viewer_enabled": bool(viewer_base_url),
            "final_section": "原文定位",
            "eligible_viewer_urls": eligible_viewer_urls,
            "required_action": (
                "Append 原文定位 as the final answer section for verified candidates actually used; "
                "if none has a complete locator, state source positioning is unavailable under uncertainty/gaps."
                if viewer_base_url
                else "No viewer section is required because viewer_base_url is not configured."
            ),
        },
        "errors": errors,
        "next_step": "Fuse with optional coarse-recall candidates, inspect governed candidates first, then run supplemental scoped exact/lexical search and verify current source/PDF evidence.",
    }
    result["duration_ms"] = round((time.monotonic_ns() - started) / 1_000_000, 3)
    if args.trace_id:
        try:
            from manage_query_trace import append_event

            append_event(
                vault_root,
                args.trace_id,
                {
                    "stage": "hierarchical-candidate-location",
                    "route": "hierarchical-search",
                    "status": result["status"],
                    "summary": "Located document/section candidates; results remain navigation-only until source verification.",
                    "hit_count": len(result["candidates"]),
                    "duration_ms": result.get("duration_ms"),
                    "inspected_paths": sorted(
                        {str(item.get("index_path")) for item in result["candidates"] if item.get("index_path")}
                    ),
                    "candidates": result["candidates"],
                },
            )
        except Exception as exc:  # Trace failure must not block retrieval.
            print(f"warning: query trace append failed: {exc}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
