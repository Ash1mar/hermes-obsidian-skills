"""SourceSection scope and non-overlapping ownership construction."""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..validation import ContractError, validate_record

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


def _fail(code: str, message: str) -> None:
    raise ContractError(code, "$", message)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer("\n", text))
    return offsets


def _line_span(text: str, start_line: int, end_line: int) -> dict[str, int]:
    offsets = _line_offsets(text)
    count = len(text.splitlines()) or 1
    if start_line < 1 or end_line < start_line or end_line > count:
        _fail("INVALID_RANGE", f"line range {start_line}-{end_line} exceeds document")
    return {"start": offsets[start_line - 1], "end": offsets[end_line] if end_line < len(offsets) else len(text)}


def build_sections(text: str, outline: Mapping[str, Any], artifact_revision: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    raw_sections = outline.get("sections")
    if not isinstance(raw_sections, list):
        _fail("INVALID_SCHEMA", "outline sections must be an array")
    line_count = len(text.splitlines()) or 1
    prepared: list[dict[str, Any]] = [{
        "id": "section-root", "parent": None, "title": "Document", "path": [], "level": 0,
        "start_line": 1, "end_line": line_count, "pages": [], "assets": [], "quality": "pass",
    }]
    ids = {"section-root"}
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            _fail("INVALID_SCHEMA", "outline section must be an object")
        section_id = str(raw.get("id", ""))
        if not ID.fullmatch(section_id) or section_id in ids:
            _fail("INVALID_SCHEMA", f"invalid or duplicate section id: {section_id}")
        ids.add(section_id)
        path = raw.get("path", [])
        prepared.append({
            "id": section_id, "parent": str(raw.get("parent") or "section-root"),
            "title": str(raw.get("title") or section_id),
            "path": [str(item) for item in path] if isinstance(path, list) else [],
            "level": int(raw.get("level", 1)), "start_line": int(raw["start_line"]),
            "end_line": int(raw["end_line"]), "pages": [int(item) for item in raw.get("pages", [])],
            "assets": [str(item) for item in raw.get("assets", [])],
            "quality": str(raw.get("quality", "pass")),
        })
    if any(item["parent"] is not None and item["parent"] not in ids for item in prepared):
        _fail("UNRESOLVED_REFERENCE", "outline contains an unknown parent section")
    by_id = {item["id"]: item for item in prepared}
    for item in prepared:
        item["scope"] = _line_span(text, item["start_line"], item["end_line"])
    children: dict[str, list[dict[str, Any]]] = {key: [] for key in by_id}
    for item in prepared[1:]:
        children[item["parent"]].append(item)
    records: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any]] = {}
    for item in prepared:
        scope, cursor, owned = item["scope"], item["scope"]["start"], []
        for child in sorted(children[item["id"]], key=lambda value: value["scope"]["start"]):
            child_scope = child["scope"]
            if child_scope["start"] < cursor or child_scope["end"] > scope["end"]:
                _fail("INVALID_OWNERSHIP", f"child section {child['id']} overlaps or escapes {item['id']}")
            if cursor < child_scope["start"]:
                owned.append({"start": cursor, "end": child_scope["start"]})
            cursor = child_scope["end"]
        if cursor < scope["end"]:
            owned.append({"start": cursor, "end": scope["end"]})
        record = {
            "contract": "hermes-source-section/v1", "section_id": item["id"],
            "parent_id": item["parent"], "artifact_revision": artifact_revision,
            "path": "document.md", "title": item["title"], "scope": scope, "owned_ranges": owned,
        }
        validate_record("section", record)
        records.append(record)
        metadata[item["id"]] = item
    coverage = sorted((span["start"], span["end"]) for record in records for span in record["owned_ranges"])
    cursor = 0
    for start, end in coverage:
        if start != cursor:
            _fail("INCOMPLETE_COVERAGE", "section ownership does not partition the document")
        cursor = end
    if cursor != len(text):
        _fail("INCOMPLETE_COVERAGE", "section ownership does not cover the document")
    return records, metadata
