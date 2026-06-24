#!/usr/bin/env python3
"""Create and maintain Bundle v2 source maps and section ingestion ledgers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEDGER_SCHEMA = "1.0"
SECTION_STATUSES = {"pending", "in_progress", "ingested", "qa_required", "skipped", "stale"}
TERMINAL_STATUSES = {"ingested", "skipped"}
TRANSITIONS = {
    "pending": {"pending", "in_progress", "qa_required", "skipped"},
    "in_progress": {"in_progress", "pending", "ingested", "qa_required", "skipped"},
    "qa_required": {"qa_required", "in_progress", "skipped"},
    "ingested": {"ingested", "qa_required", "stale"},
    "skipped": {"skipped", "pending", "in_progress"},
    "stale": {"stale", "in_progress", "qa_required", "skipped"},
}


class LedgerError(RuntimeError):
    """Raised for controlled ledger failures."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"Expected a JSON object: {path}")
    return value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE).strip("-.")
    return cleaned or "document"


def load_validator() -> Any:
    validator_path = Path(__file__).with_name("validate_document_bundle.py")
    spec = importlib.util.spec_from_file_location("bundle_validator", validator_path)
    if spec is None or spec.loader is None:
        raise LedgerError(f"Cannot load bundle validator: {validator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_bundle(bundle: Path) -> dict[str, Any]:
    return load_validator().validate_bundle(bundle)


def section_text(lines: list[str], start: int, end: int) -> str:
    return "".join(lines[start - 1 : end])


def normalize_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def make_section_entry(section: dict[str, Any], lines: list[str], validation_status: str) -> dict[str, Any]:
    section_id = str(section.get("id", "")).strip()
    if not section_id:
        raise LedgerError("outline.json contains a section without an id")
    try:
        start = int(section["start_line"])
        end = int(section["end_line"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError(f"Section {section_id} has an invalid line range") from exc
    if start < 1 or end < start or end > len(lines):
        raise LedgerError(f"Section {section_id} range {start}-{end} is outside document lines")

    section_quality = str(section.get("quality", "pass"))
    initial_status = "pending"
    if validation_status == "fail" or section_quality != "pass":
        initial_status = "qa_required"

    return {
        "id": section_id,
        "title": str(section.get("title", section_id)),
        "level": int(section.get("level", 1)),
        "parent": section.get("parent"),
        "path": [str(item) for item in normalize_list(section.get("path"))],
        "start_line": start,
        "end_line": end,
        "pages": [int(item) for item in normalize_list(section.get("pages"))],
        "assets": [str(item) for item in normalize_list(section.get("assets"))],
        "quality": section_quality,
        "scope_sha256": sha256_bytes(section_text(lines, start, end).encode("utf-8")),
        "children": [],
        "content_ranges": [],
        "ingest_unit": True,
        "status": initial_status,
        "outputs": [],
        "qa_items": [],
        "notes": [],
        "started_at": None,
        "completed_at": None,
        "updated_at": None,
    }


def assign_content_ranges(sections: list[dict[str, Any]], lines: list[str]) -> None:
    """Assign each outline node the lines it owns, excluding direct child scopes."""
    by_id = {str(section["id"]): section for section in sections}
    for section in sections:
        parent_id = section.get("parent")
        if parent_id in by_id:
            by_id[str(parent_id)]["children"].append(section["id"])

    for section in sections:
        start = int(section["start_line"])
        end = int(section["end_line"])
        child_ranges: list[tuple[int, int]] = []
        for child_id in section["children"]:
            child = by_id[str(child_id)]
            child_start = int(child["start_line"])
            child_end = int(child["end_line"])
            if child_start < start or child_end > end:
                raise LedgerError(f"Child section {child_id} falls outside parent {section['id']}")
            child_ranges.append((child_start, child_end))

        owned_ranges: list[dict[str, int]] = []
        cursor = start
        for child_start, child_end in sorted(child_ranges):
            if cursor < child_start:
                owned_ranges.append({"start_line": cursor, "end_line": child_start - 1})
            cursor = max(cursor, child_end + 1)
        if cursor <= end:
            owned_ranges.append({"start_line": cursor, "end_line": end})

        section["content_ranges"] = owned_ranges
        section["ingest_unit"] = bool(owned_ranges)
        owned_content = [
            {
                "start_line": item["start_line"],
                "end_line": item["end_line"],
                "text": section_text(lines, item["start_line"], item["end_line"]),
            }
            for item in owned_ranges
        ]
        section["content_sha256"] = sha256_bytes(
            json.dumps(owned_content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )


def snapshot_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    manifest_path = bundle / "manifest.json"
    manifest = load_json(manifest_path)
    if not str(manifest.get("schema_version", "")).startswith("2"):
        raise LedgerError("Section ledgers require a Bundle v2 manifest")

    document_info = manifest.get("document")
    outline_info = manifest.get("outline")
    if not isinstance(document_info, dict) or not isinstance(outline_info, dict):
        raise LedgerError("Bundle v2 manifest must declare document and outline objects")
    document_path = bundle / str(document_info.get("path", "document.md"))
    outline_path = bundle / str(outline_info.get("path", "outline.json"))
    document_text = document_path.read_text(encoding="utf-8-sig")
    lines = document_text.splitlines(keepends=True)
    outline = load_json(outline_path)
    outline_sections = outline.get("sections")
    if not isinstance(outline_sections, list):
        raise LedgerError("outline.json sections must be a list")

    validation = validate_bundle(bundle)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    source_sha = str(source.get("sha256", ""))
    if not source_sha:
        raise LedgerError("manifest.source.sha256 is required for controlled ingestion")
    source_filename = str(source.get("filename", bundle.name))
    source_stem = Path(source_filename).stem
    bundle_id = f"bundle-v2-{source_sha[:16]}"
    sections = [make_section_entry(item, lines, str(validation["status"])) for item in outline_sections]
    assign_content_ranges(sections, lines)

    return {
        "bundle": bundle,
        "manifest": manifest,
        "manifest_sha256": sha256_file(manifest_path),
        "document_sha256": sha256_file(document_path),
        "outline_sha256": sha256_file(outline_path),
        "bundle_id": bundle_id,
        "source_sha256": source_sha,
        "source_filename": source_filename,
        "source_stem": source_stem,
        "sections": sections,
        "validation": validation,
    }


def ledger_state(ledger: dict[str, Any]) -> str:
    validation = ledger.get("validation")
    if isinstance(validation, dict) and validation.get("status") == "fail":
        return "blocked"
    ingest_units = [
        item
        for item in normalize_list(ledger.get("sections"))
        if isinstance(item, dict) and item.get("ingest_unit", True)
    ]
    statuses = {str(item.get("status")) for item in ingest_units}
    if "stale" in statuses:
        return "stale"
    if statuses and statuses.issubset(TERMINAL_STATUSES):
        return "complete"
    return "active"


def summarize(ledger: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(SECTION_STATUSES)}
    for section in normalize_list(ledger.get("sections")):
        status = str(section.get("status"))
        counts[status] = counts.get(status, 0) + 1
    ingest_units = [
        item
        for item in normalize_list(ledger.get("sections"))
        if isinstance(item, dict) and item.get("ingest_unit", True)
    ]
    counts["ingest_units_total"] = len(ingest_units)
    counts["ingest_units_remaining"] = sum(
        1 for item in ingest_units if str(item.get("status")) not in TERMINAL_STATUSES
    )
    return counts


def append_history(ledger: dict[str, Any], event: dict[str, Any]) -> None:
    history = ledger.setdefault("history", [])
    if not isinstance(history, list):
        ledger["history"] = history = []
    history.append({"at": utc_now(), **event})


def preserve_section_state(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    if new["content_sha256"] != old.get("content_sha256"):
        previous_status = str(old.get("status", "pending"))
        if previous_status in {"ingested", "skipped", "in_progress", "stale"}:
            new["status"] = "stale"
        elif previous_status == "qa_required":
            new["status"] = "qa_required"
        else:
            new["status"] = "pending"
        new["previous_status"] = previous_status
        new["outputs"] = normalize_list(old.get("outputs"))
        new["qa_items"] = normalize_list(old.get("qa_items"))
        new["notes"] = normalize_list(old.get("notes")) + ["Section content changed since the previous ledger revision."]
        new["updated_at"] = utc_now()
        return new

    for field in (
        "status",
        "outputs",
        "qa_items",
        "notes",
        "started_at",
        "completed_at",
        "updated_at",
        "previous_status",
    ):
        if field in old:
            new[field] = old[field]
    return new


def build_ledger(
    snapshot: dict[str, Any],
    ledger_path: Path,
    source_map_path: Path,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    now = utc_now()
    manifest = snapshot["manifest"]
    old_sections: dict[str, dict[str, Any]] = {}
    revision = 1
    created_at = now
    history: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []

    if existing is not None:
        if existing.get("bundle_id") != snapshot["bundle_id"]:
            raise LedgerError("Existing ledger belongs to a different source SHA; use --replace intentionally")
        revision = int(existing.get("revision", 0)) + 1
        created_at = str(existing.get("created_at", now))
        history = normalize_list(existing.get("history"))
        orphaned = normalize_list(existing.get("orphaned_sections"))
        old_sections = {
            str(item.get("id")): item
            for item in normalize_list(existing.get("sections"))
            if isinstance(item, dict) and item.get("id")
        }

    sections = []
    current_ids: set[str] = set()
    changed_ids: list[str] = []
    for section in snapshot["sections"]:
        section_id = str(section["id"])
        current_ids.add(section_id)
        if section_id in old_sections:
            previous_hash = old_sections[section_id].get("content_sha256")
            section = preserve_section_state(section, old_sections[section_id])
            if section["content_sha256"] != previous_hash:
                changed_ids.append(section_id)
        sections.append(section)

    for section_id, previous in old_sections.items():
        if section_id not in current_ids:
            removed = dict(previous)
            removed["removed_at"] = now
            removed["status"] = "stale"
            orphaned.append(removed)

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    ledger = {
        "ledger_schema_version": LEDGER_SCHEMA,
        "bundle_id": snapshot["bundle_id"],
        "revision": revision,
        "state": "active",
        "created_at": created_at,
        "updated_at": now,
        "control": {
            "ledger_path": str(ledger_path.resolve()),
            "source_map_path": str(source_map_path.resolve()),
        },
        "bundle": {
            "path": str(snapshot["bundle"]),
            "schema_version": manifest.get("schema_version"),
            "profile": manifest.get("profile"),
            "manifest_sha256": snapshot["manifest_sha256"],
            "document_sha256": snapshot["document_sha256"],
            "outline_sha256": snapshot["outline_sha256"],
        },
        "source": {
            "filename": snapshot["source_filename"],
            "path": (manifest.get("source") or {}).get("path"),
            "sha256": snapshot["source_sha256"],
        },
        "conversion": manifest.get("conversion", {}),
        "validation": snapshot["validation"],
        "review_required": normalize_list(quality.get("review_required")),
        "sections": sections,
        "orphaned_sections": orphaned,
        "history": history,
    }
    append_history(
        ledger,
        {
            "action": "initialized" if existing is None else "reconciled",
            "revision": revision,
            "changed_sections": changed_ids,
        },
    )
    ledger["state"] = ledger_state(ledger)
    ledger["summary"] = summarize(ledger)
    return ledger


def yaml_string(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def display_pages(value: Any) -> str:
    pages = normalize_list(value)
    return ", ".join(str(item) for item in pages) if pages else "-"


def render_source_map(ledger: dict[str, Any]) -> str:
    source = ledger.get("source") if isinstance(ledger.get("source"), dict) else {}
    bundle = ledger.get("bundle") if isinstance(ledger.get("bundle"), dict) else {}
    validation = ledger.get("validation") if isinstance(ledger.get("validation"), dict) else {}
    control = ledger.get("control") if isinstance(ledger.get("control"), dict) else {}
    ledger_name = Path(str(control.get("ledger_path", "section-ledger.json"))).name
    sections = normalize_list(ledger.get("sections"))

    lines = [
        "---",
        "type: source-map",
        f"bundle_id: {yaml_string(ledger.get('bundle_id'))}",
        f"bundle_schema: {yaml_string(bundle.get('schema_version'))}",
        f"bundle_profile: {yaml_string(bundle.get('profile'))}",
        f"source_sha256: {yaml_string(source.get('sha256'))}",
        f"document_sha256: {yaml_string(bundle.get('document_sha256'))}",
        f"validation_status: {yaml_string(validation.get('status'))}",
        f"ledger_revision: {int(ledger.get('revision', 0))}",
        f"ingest_state: {yaml_string(ledger.get('state'))}",
        f"updated: {yaml_string(ledger.get('updated_at'))}",
        "---",
        "",
        f"# Source Map — {source.get('filename', ledger.get('bundle_id'))}",
        "",
        "## Control",
        "",
        f"- Bundle: `{bundle.get('path', '')}`",
        f"- Section ledger: [{ledger_name}](./{ledger_name})",
        f"- Validation: `{validation.get('status', 'unknown')}`",
        f"- Ledger revision: `{ledger.get('revision', 0)}`",
        f"- Ingest state: `{ledger.get('state', 'unknown')}`",
        "",
        "## Quality Gate",
        "",
    ]
    review_required = normalize_list(ledger.get("review_required"))
    issues = normalize_list(validation.get("issues"))
    if not review_required and not issues:
        lines.append("- No bundle-level review requirement.")
    for item in review_required:
        lines.append(f"- Review required: `{item}`")
    for issue in issues:
        if isinstance(issue, dict):
            lines.append(
                f"- `{issue.get('severity', 'info')}` `{issue.get('code', 'issue')}`: {issue.get('message', '')}"
            )

    lines.extend(
        [
            "",
            "## Section Ledger View",
            "",
            "| Status | Section ID | Title | Owned lines | Scope | Pages | Quality | Outputs |",
            "|---|---|---|---|---:|---|---|---|",
        ]
    )
    for section in sections:
        outputs = normalize_list(section.get("outputs"))
        lines.append(
            "| {status} | `{sid}` | {title} | {owned} | {start}–{end} | {pages} | {quality} | {outputs} |".format(
                status=escape_cell(section.get("status")),
                sid=escape_cell(section.get("id")),
                title=escape_cell(section.get("title")),
                owned=escape_cell(
                    ", ".join(
                        f"{item.get('start_line')}–{item.get('end_line')}"
                        for item in normalize_list(section.get("content_ranges"))
                        if isinstance(item, dict)
                    )
                    or "container"
                ),
                start=section.get("start_line"),
                end=section.get("end_line"),
                pages=escape_cell(display_pages(section.get("pages"))),
                quality=escape_cell(section.get("quality")),
                outputs=escape_cell(", ".join(str(item) for item in outputs) or "-"),
            )
        )

    qa_sections = [
        item for item in sections if normalize_list(item.get("qa_items")) or item.get("status") in {"qa_required", "stale"}
    ]
    lines.extend(["", "## QA and Stale Sections", ""])
    if not qa_sections:
        lines.append("- None.")
    for section in qa_sections:
        lines.append(f"### {section.get('id')} — {section.get('title')}")
        lines.append("")
        lines.append(f"- Status: `{section.get('status')}`")
        for item in normalize_list(section.get("qa_items")):
            lines.append(f"- QA: {item}")
        for item in normalize_list(section.get("notes")):
            lines.append(f"- Note: {item}")
        lines.append("")

    lines.extend(
        [
            "## Citation Contract",
            "",
            "Every downstream note created from this bundle must record:",
            "",
            "- `source_bundle_id`",
            "- `source_sha256`",
            "- `source_section_id`",
            "- `source_lines`",
            "- `source_pages`",
            "- linked table/image IDs when used",
            "",
            "Do not cite `_evidence/` as normalized content; use it only to resolve a QA issue.",
            "",
        ]
    )
    return "\n".join(lines)


def resolve_control_paths(args: argparse.Namespace, snapshot: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = args.reports_dir.expanduser().resolve()
    stem = safe_filename(snapshot["source_stem"])
    ledger_path = (args.ledger or reports_dir / f"{stem}.section-ledger.json").expanduser().resolve()
    source_map_path = (args.source_map or reports_dir / f"{stem}.source-map.md").expanduser().resolve()
    return ledger_path, source_map_path


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = snapshot_bundle(args.bundle)
    ledger_path, source_map_path = resolve_control_paths(args, snapshot)
    existing = None
    if ledger_path.exists() and not args.replace:
        existing = load_json(ledger_path)
    ledger = build_ledger(snapshot, ledger_path, source_map_path, existing)
    atomic_write_json(ledger_path, ledger)
    atomic_write_text(source_map_path, render_source_map(ledger))
    return {
        "ledger": str(ledger_path),
        "source_map": str(source_map_path),
        "bundle_id": ledger["bundle_id"],
        "revision": ledger["revision"],
        "state": ledger["state"],
        "validation_status": ledger["validation"]["status"],
        "summary": ledger["summary"],
    }


def find_section(ledger: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in normalize_list(ledger.get("sections")):
        if isinstance(section, dict) and section.get("id") == section_id:
            return section
    raise LedgerError(f"Unknown section id: {section_id}")


def merge_unique(existing: Any, values: list[str]) -> list[str]:
    result = [str(item) for item in normalize_list(existing)]
    for value in values:
        if value not in result:
            result.append(value)
    return result


def command_update(args: argparse.Namespace) -> dict[str, Any]:
    ledger_path = args.ledger.expanduser().resolve()
    ledger = load_json(ledger_path)
    revision = int(ledger.get("revision", 0))
    if args.expected_revision is not None and args.expected_revision != revision:
        raise LedgerError(f"Revision conflict: expected {args.expected_revision}, current {revision}")

    section = find_section(ledger, args.section)
    old_status = str(section.get("status", "pending"))
    new_status = args.status or old_status
    if new_status not in SECTION_STATUSES:
        raise LedgerError(f"Unsupported status: {new_status}")
    if not section.get("ingest_unit", True) and not args.force_transition:
        raise LedgerError(f"Section {args.section} has no owned content ranges and is not an ingest unit")
    if not args.force_transition and new_status not in TRANSITIONS.get(old_status, set()):
        raise LedgerError(f"Invalid transition: {old_status} -> {new_status}")

    outputs = args.output or []
    qa_items = args.qa_item or []
    if new_status == "ingested" and not outputs and not normalize_list(section.get("outputs")):
        raise LedgerError("An ingested section must record at least one --output")
    if new_status == "skipped" and not args.note:
        raise LedgerError("A skipped section must record --note")
    if new_status == "qa_required" and not qa_items and not args.note and not normalize_list(section.get("qa_items")):
        raise LedgerError("A qa_required section must record --qa-item or --note")

    now = utc_now()
    section["status"] = new_status
    section["outputs"] = merge_unique(section.get("outputs"), outputs)
    section["qa_items"] = merge_unique(section.get("qa_items"), qa_items)
    if args.note:
        section["notes"] = merge_unique(section.get("notes"), [args.note])
    if new_status == "in_progress" and not section.get("started_at"):
        section["started_at"] = now
    if new_status in TERMINAL_STATUSES:
        section["completed_at"] = now
    section["updated_at"] = now

    ledger["revision"] = revision + 1
    ledger["updated_at"] = now
    append_history(
        ledger,
        {
            "action": "section_updated",
            "revision": ledger["revision"],
            "section_id": args.section,
            "from": old_status,
            "to": new_status,
            "outputs": outputs,
            "note": args.note,
        },
    )
    ledger["state"] = ledger_state(ledger)
    ledger["summary"] = summarize(ledger)
    atomic_write_json(ledger_path, ledger)

    control = ledger.get("control") if isinstance(ledger.get("control"), dict) else {}
    source_map_path = (
        args.source_map.expanduser().resolve()
        if args.source_map
        else Path(str(control.get("source_map_path", ledger_path.with_suffix(".source-map.md"))))
    )
    atomic_write_text(source_map_path, render_source_map(ledger))
    return {
        "ledger": str(ledger_path),
        "source_map": str(source_map_path),
        "revision": ledger["revision"],
        "state": ledger["state"],
        "section_id": args.section,
        "section_status": new_status,
        "summary": ledger["summary"],
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    ledger_path = args.ledger.expanduser().resolve()
    ledger = load_json(ledger_path)
    result: dict[str, Any] = {
        "ledger": str(ledger_path),
        "bundle_id": ledger.get("bundle_id"),
        "revision": ledger.get("revision"),
        "state": ledger.get("state"),
        "validation_status": (ledger.get("validation") or {}).get("status"),
        "summary": summarize(ledger),
    }
    if args.section:
        result["section"] = find_section(ledger, args.section)
    else:
        result["sections"] = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "lines": [item.get("start_line"), item.get("end_line")],
                "pages": item.get("pages"),
                "outputs": item.get("outputs"),
            }
            for item in normalize_list(ledger.get("sections"))
            if isinstance(item, dict)
        ]
    return result


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    for key in ("bundle_id", "ledger", "source_map", "revision", "state", "validation_status"):
        if key in result:
            print(f"{key}: {result[key]}")
    if "section_id" in result:
        print(f"section: {result['section_id']} -> {result['section_status']}")
    summary = result.get("summary")
    if isinstance(summary, dict):
        print("summary: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    for section in normalize_list(result.get("sections")):
        print(
            f"- {section.get('id')} [{section.get('status')}] "
            f"lines {section.get('lines')} pages {section.get('pages')}: {section.get('title')}"
        )
    if "section" in result:
        print(json.dumps(result["section"], ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Bundle v2 source maps and section ledgers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or reconcile a source map and section ledger")
    init_parser.add_argument("bundle", type=Path, help="Bundle v2 directory")
    init_parser.add_argument("--reports-dir", type=Path, required=True, help="Vault _system/reports directory")
    init_parser.add_argument("--ledger", type=Path, help="Explicit ledger path")
    init_parser.add_argument("--source-map", type=Path, help="Explicit source-map path")
    init_parser.add_argument("--replace", action="store_true", help="Replace rather than reconcile an existing ledger")
    init_parser.add_argument("--json", action="store_true", help="Print JSON result")
    init_parser.set_defaults(handler=command_init)

    update_parser = subparsers.add_parser("update", help="Update one section with revision checking")
    update_parser.add_argument("ledger", type=Path, help="Section ledger JSON")
    update_parser.add_argument("--section", required=True, help="Section id")
    update_parser.add_argument("--status", choices=sorted(SECTION_STATUSES), help="New section status")
    update_parser.add_argument("--output", action="append", help="Created or updated output path; repeatable")
    update_parser.add_argument("--qa-item", action="append", help="QA item; repeatable")
    update_parser.add_argument("--note", help="Decision or transition note")
    update_parser.add_argument("--expected-revision", type=int, help="Fail if the ledger revision changed")
    update_parser.add_argument("--source-map", type=Path, help="Override source-map output path")
    update_parser.add_argument("--force-transition", action="store_true", help="Allow a non-standard status transition")
    update_parser.add_argument("--json", action="store_true", help="Print JSON result")
    update_parser.set_defaults(handler=command_update)

    status_parser = subparsers.add_parser("status", help="Show ledger summary or one section")
    status_parser.add_argument("ledger", type=Path, help="Section ledger JSON")
    status_parser.add_argument("--section", help="Show one section")
    status_parser.add_argument("--json", action="store_true", help="Print JSON result")
    status_parser.set_defaults(handler=command_status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except (LedgerError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
