#!/usr/bin/env python3
"""Locate source-section candidates without treating the query projection as evidence.

Design origin: hanyu. This is a parallel candidate locator inside the existing
controlled-query source-navigation stage, not a second answer pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


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
    return sum(weight * max(1, len(term) - 1) for term in matched), matched


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


def diversify_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Prevent one repetitive document from consuming the entire section window."""
    bounded_limit = max(1, limit)
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    covered_documents: set[str] = set()
    target_documents = min(3, bounded_limit)
    for candidate in candidates:
        document_path = str(candidate.get("document_path") or "")
        if document_path in covered_documents:
            continue
        selected.append(candidate)
        selected_keys.add((document_path, str(candidate.get("section_id") or "")))
        covered_documents.add(document_path)
        if len(covered_documents) >= target_documents:
            break
    for candidate in candidates:
        key = (str(candidate.get("document_path") or ""), str(candidate.get("section_id") or ""))
        if key in selected_keys:
            continue
        selected.append(candidate)
        selected_keys.add(key)
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
    args = parser.parse_args()
    started = time.monotonic_ns()

    vault_root = args.vault_root.resolve()
    index_dir = (args.index_dir or vault_root / "_system" / "reports" / "query-index").resolve()
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
            title_score, title_matches = match_score(terms, str(section.get("title", "")), 9)
            path_score, path_matches = match_score(terms, " / ".join(section.get("path_titles", [])), 4)
            content_score = 0
            content_matches: list[str] = []
            if document_lines is not None:
                content = read_ranges(document_lines, section.get("content_ranges", []))
                content_score, content_matches = match_score(terms, content, 2)
            score = document_score + title_score + path_score + content_score
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
    result = {
        "status": "warn" if errors else "ok",
        "authority": "candidate-navigation-only",
        "design_origin": "hanyu",
        "query": args.query,
        "terms": compact_terms(terms, limit=12),
        "candidates": selected_candidates,
        "ranking": {
            "strategy": "score-with-document-diversity",
            "document_count": len({str(item.get("document_path") or "") for item in selected_candidates}),
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
