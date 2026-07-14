#!/usr/bin/env python3
"""Locate source-section candidates without treating the query projection as evidence.

Design origin: hanyu. This is a parallel candidate locator inside the existing
controlled-query source-navigation stage, not a second answer pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
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


def match_score(terms: list[str], text: str, weight: int) -> tuple[int, list[str]]:
    folded = text.casefold()
    matched = [term for term in terms if term in folded]
    return sum(weight * max(1, len(term) - 1) for term in matched), matched


def read_ranges(document: Path, ranges: list[dict[str, Any]]) -> str:
    lines = document.read_text(encoding="utf-8", errors="replace").splitlines()
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("query")
    parser.add_argument("--top-documents", type=int, default=8)
    parser.add_argument("--top-sections", type=int, default=20)
    parser.add_argument("--index-dir", type=Path, help="Defaults to <vault>/_system/reports/query-index")
    parser.add_argument("--no-content-scan", action="store_true", help="Score only document routing and section paths")
    args = parser.parse_args()

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
        for section in projection.get("sections", []):
            title_score, title_matches = match_score(terms, str(section.get("title", "")), 9)
            path_score, path_matches = match_score(terms, " / ".join(section.get("path_titles", [])), 4)
            content_score = 0
            content_matches: list[str] = []
            if not args.no_content_scan and source_document.is_file():
                try:
                    content = read_ranges(source_document, section.get("content_ranges", []))
                    content_score, content_matches = match_score(terms, content, 2)
                except OSError as exc:
                    errors.append(f"{source_document}: {exc}")
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
                        "document": document_matches,
                        "title": title_matches,
                        "path": path_matches,
                        "content": content_matches,
                    },
                }
            )

    candidates.sort(key=lambda item: (-int(item["score"]), str(item["source_filename"]), str(item["section_id"])))
    result = {
        "status": "warn" if errors else "ok",
        "authority": "candidate-navigation-only",
        "design_origin": "hanyu",
        "query": args.query,
        "terms": terms,
        "candidates": candidates[: max(1, args.top_sections)],
        "errors": errors,
        "next_step": "Merge with existing report-navigation hits, then verify document.md and page/table/figure evidence before answering.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
