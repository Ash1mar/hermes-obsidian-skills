#!/usr/bin/env python
"""Validate a Hermes engineering document bundle without modifying it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def add_issue(issues: list[dict[str, Any]], code: str, severity: str, message: str) -> None:
    issues.append({"code": code, "severity": severity, "message": message})


def load_json(path: Path, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        add_issue(issues, "invalid-json", "fail", f"Cannot read {path.name}: {exc}")
        return None
    if not isinstance(data, dict):
        add_issue(issues, "invalid-json-shape", "fail", f"{path.name} must contain an object")
        return None
    return data


def validate_asset_paths(
    bundle: Path,
    values: Any,
    label: str,
    issues: list[dict[str, Any]],
) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        add_issue(issues, f"invalid-{label}-list", "fail", f"manifest.{label} must be a list")
        return
    seen_ids: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            add_issue(issues, f"invalid-{label}-entry", "fail", f"{label}[{index}] must be an object")
            continue
        item_id = str(value.get("id", ""))
        if not item_id:
            add_issue(issues, f"missing-{label}-id", "fail", f"{label}[{index}] has no id")
        elif item_id in seen_ids:
            add_issue(issues, f"duplicate-{label}-id", "fail", f"Duplicate {label} id: {item_id}")
        else:
            seen_ids.add(item_id)
        for key in ("path", "evidence_path"):
            relative = value.get(key)
            if not relative:
                continue
            target = bundle / str(relative)
            if not target.is_file() or target.stat().st_size == 0:
                add_issue(
                    issues,
                    f"missing-{label}-asset",
                    "fail",
                    f"{label} {item_id or index} references missing or empty {key}: {relative}",
                )


def validate_outline(
    bundle: Path,
    manifest: dict[str, Any],
    line_count: int,
    issues: list[dict[str, Any]],
) -> int:
    outline_info = manifest.get("outline")
    if not isinstance(outline_info, dict):
        add_issue(issues, "missing-outline", "fail", "Bundle v2 requires manifest.outline")
        return 0
    relative = outline_info.get("path")
    if not relative:
        add_issue(issues, "missing-outline-path", "fail", "manifest.outline.path is required")
        return 0
    outline = load_json(bundle / str(relative), issues)
    if outline is None:
        return 0
    sections = outline.get("sections")
    if not isinstance(sections, list) or not sections:
        add_issue(issues, "empty-outline", "warn", "outline.json has no sections")
        return 0

    ids: set[str] = set()
    previous_start = 0
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            add_issue(issues, "invalid-section", "fail", f"sections[{index}] must be an object")
            continue
        section_id = str(section.get("id", ""))
        if not section_id:
            add_issue(issues, "missing-section-id", "fail", f"sections[{index}] has no id")
        elif section_id in ids:
            add_issue(issues, "duplicate-section-id", "fail", f"Duplicate section id: {section_id}")
        else:
            ids.add(section_id)
        try:
            start = int(section.get("start_line", 0))
            end = int(section.get("end_line", 0))
        except (TypeError, ValueError):
            add_issue(issues, "invalid-section-range", "fail", f"Invalid line range for {section_id or index}")
            continue
        if start < 1 or end < start or end > line_count:
            add_issue(
                issues,
                "invalid-section-range",
                "fail",
                f"Section {section_id or index} range {start}-{end} is outside document lines 1-{line_count}",
            )
        if start < previous_start:
            add_issue(issues, "unordered-sections", "fail", "outline sections are not ordered by start_line")
        previous_start = start
    return len(sections)


def validate_bundle(bundle: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not bundle.is_dir():
        add_issue(issues, "missing-bundle", "fail", f"Bundle directory does not exist: {bundle}")
        return {"status": "fail", "issues": issues}

    manifest_path = bundle / "manifest.json"
    document_path = bundle / "document.md"
    if not manifest_path.is_file():
        add_issue(issues, "missing-manifest", "fail", "manifest.json is required")
    if not document_path.is_file() or document_path.stat().st_size == 0:
        add_issue(issues, "missing-document", "fail", "document.md is required and must be non-empty")
    if issues:
        return {"status": "fail", "issues": issues}

    manifest = load_json(manifest_path, issues)
    if manifest is None:
        return {"status": "fail", "issues": issues}

    schema = str(manifest.get("schema_version", "1.0"))
    document_text = document_path.read_text(encoding="utf-8-sig")
    line_count = len(document_text.splitlines())
    section_count = 0

    validate_asset_paths(bundle, manifest.get("images"), "images", issues)
    validate_asset_paths(bundle, manifest.get("tables"), "tables", issues)

    if schema.startswith("2"):
        if manifest.get("profile") not in {"basic", "engineering"}:
            add_issue(issues, "invalid-profile", "fail", "Bundle v2 profile must be basic or engineering")
        section_count = validate_outline(bundle, manifest, line_count, issues)
        evidence = manifest.get("evidence")
        if isinstance(evidence, dict) and evidence.get("default_ingest") is not False:
            add_issue(
                issues,
                "evidence-default-ingest-enabled",
                "fail",
                "_evidence must not be part of default controlled ingestion",
            )
        if "<!-- source-page:" not in document_text and manifest.get("profile") == "engineering":
            add_issue(issues, "missing-page-anchors", "warn", "Engineering document has no page anchors")
    else:
        add_issue(
            issues,
            "legacy-bundle-limited-evidence",
            "warn",
            "Bundle v1 supports coarse source mapping only; text, table, and formula page evidence is incomplete.",
        )

    manifest_quality = manifest.get("quality")
    if isinstance(manifest_quality, dict):
        for issue in manifest_quality.get("issues", []):
            if isinstance(issue, dict) and issue not in issues:
                issues.append(issue)

    severities = {str(issue.get("severity")) for issue in issues}
    status = "fail" if "fail" in severities else "warn" if "warn" in severities else "pass"
    return {
        "bundle": str(bundle),
        "schema_version": schema,
        "profile": manifest.get("profile"),
        "status": status,
        "line_count": line_count,
        "section_count": section_count,
        "image_count": len(manifest.get("images", [])) if isinstance(manifest.get("images"), list) else 0,
        "table_count": len(manifest.get("tables", [])) if isinstance(manifest.get("tables"), list) else 0,
        "review_required": (
            manifest_quality.get("review_required", []) if isinstance(manifest_quality, dict) else []
        ),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Hermes document bundle")
    parser.add_argument("bundle", type=Path, help="Path to the document bundle")
    parser.add_argument("--json", action="store_true", help="Print the complete JSON result")
    args = parser.parse_args()

    result = validate_bundle(args.bundle.expanduser().resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Bundle: {result.get('bundle', args.bundle)}")
        print(f"Schema: {result.get('schema_version', 'unknown')}")
        print(f"Status: {result['status']}")
        print(f"Sections: {result.get('section_count', 0)}")
        print(f"Images: {result.get('image_count', 0)}")
        print(f"Tables: {result.get('table_count', 0)}")
        for issue in result.get("issues", []):
            print(f"- [{issue.get('severity', 'info')}] {issue.get('code')}: {issue.get('message')}")

    return {"pass": 0, "warn": 1, "fail": 2}[str(result["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
