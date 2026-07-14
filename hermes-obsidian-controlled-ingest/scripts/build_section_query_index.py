#!/usr/bin/env python3
"""Build non-authoritative section query projections from existing Bundle v2 outputs.

Design origin: hanyu. The projection is disposable and never replaces the
manifest, outline, section ledger, source map, spec index, or source document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def stable_terms(*values: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        for term in re.findall(r"[A-Za-z]+\d*[A-Za-z0-9._/-]*|\d+[A-Za-z][A-Za-z0-9._/-]*|[\u3400-\u9fff]{2,}", value):
            normalized = term.casefold()
            if normalized not in seen:
                seen.add(normalized)
                terms.append(term)
    return terms


def find_ledgers(vault_root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    reports = vault_root / "_system" / "reports"
    for path in reports.glob("*.section-ledger.json"):
        try:
            ledger = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        bundle_name = Path(str(ledger.get("bundle", {}).get("path", ""))).name
        if bundle_name:
            result[bundle_name] = (path, ledger)
    return result


def spec_index_paths(ledger: dict[str, Any], vault_root: Path) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for section in ledger.get("sections", []):
        for output in section.get("outputs", []):
            if str(output).endswith(".spec-index.md") and str(output) not in seen:
                seen.add(str(output))
                paths.append(str(output))
    return paths


def projection_name(bundle: Path) -> str:
    name = bundle.name
    for suffix in ("_image_document_bundle", "_document_bundle"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    safe = re.sub(r"[^A-Za-z0-9._\-\u3400-\u9fff]+", "-", name).strip("-")
    return f"{safe or 'document'}.section-query-index.json"


def build_projection(
    bundle: Path,
    vault_root: Path,
    ledger_entry: tuple[Path, dict[str, Any]] | None,
) -> dict[str, Any]:
    manifest_path = bundle / "manifest.json"
    outline_path = bundle / "outline.json"
    manifest = load_json(manifest_path)
    outline = load_json(outline_path)
    ledger_path: Path | None = None
    ledger: dict[str, Any] = {}
    if ledger_entry:
        ledger_path, ledger = ledger_entry

    source = manifest.get("source", {})
    source_filename = str(source.get("filename") or Path(str(source.get("path", ""))).name)
    document_rel = relative_path(bundle / str(manifest.get("document", {}).get("path", "document.md")), vault_root)
    specs = spec_index_paths(ledger, vault_root)
    routing_values = [source_filename, bundle.name, str(manifest.get("profile", "")), *specs]
    manifest_routing = manifest.get("routing", {})
    if isinstance(manifest_routing, dict):
        for value in manifest_routing.values():
            if isinstance(value, list):
                routing_values.extend(str(item) for item in value)
            elif value is not None:
                routing_values.append(str(value))

    ledger_sections = {str(item.get("id")): item for item in ledger.get("sections", [])}
    outline_sections = outline.get("sections", [])
    titles = {str(item.get("id")): str(item.get("title", "")) for item in outline_sections}
    sections: list[dict[str, Any]] = []
    for section in outline_sections:
        section_id = str(section.get("id"))
        state = ledger_sections.get(section_id, {})
        path_ids = [str(item) for item in section.get("path", [])]
        ranges = state.get("content_ranges") or [
            {"start_line": section.get("start_line"), "end_line": section.get("end_line")}
        ]
        sections.append(
            {
                "section_id": section_id,
                "title": section.get("title", ""),
                "level": section.get("level"),
                "parent": section.get("parent"),
                "path": path_ids,
                "path_titles": [titles[item] for item in path_ids if item in titles],
                "start_line": section.get("start_line"),
                "end_line": section.get("end_line"),
                "content_ranges": ranges,
                "pages": section.get("pages", []),
                "assets": section.get("assets", []),
                "quality": section.get("quality", "unknown"),
                "ingest_status": state.get("status", "untracked"),
                "content_hash": state.get("content_sha256") or state.get("scope_sha256"),
            }
        )

    source_state = {
        "bundle_schema_version": manifest.get("schema_version"),
        "ledger_revision": ledger.get("revision"),
        "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "outline_hash": hashlib.sha256(outline_path.read_bytes()).hexdigest(),
        "document_hash": ledger.get("bundle", {}).get("document_sha256"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "section-query-index",
        "authority": "non-authoritative",
        "rebuildable": True,
        "design_origin": "hanyu",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "document": {
            "document_id": ledger.get("bundle_id") or bundle.name,
            "source_filename": source_filename,
            "source_path": relative_path(Path(str(source.get("path", source_filename))), vault_root),
            "bundle_path": relative_path(bundle, vault_root),
            "document_path": document_rel,
            "profile": manifest.get("profile"),
            "spec_indexes": specs,
            "routing_terms": stable_terms(*routing_values),
            "routing_metadata": manifest_routing if isinstance(manifest_routing, dict) else {},
        },
        "source_state": source_state,
        "ledger_path": relative_path(ledger_path, vault_root) if ledger_path else None,
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("--bundle", action="append", type=Path, help="Index only this Bundle v2 directory; repeatable")
    parser.add_argument("--output-dir", type=Path, help="Defaults to <vault>/_system/reports/query-index")
    parser.add_argument("--check", action="store_true", help="Build and validate projections without writing files")
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    output_dir = (args.output_dir or vault_root / "_system" / "reports" / "query-index").resolve()
    bundles = [path.resolve() for path in args.bundle] if args.bundle else [
        path.parent for path in (vault_root / "10_Raw" / "converted").rglob("manifest.json")
        if (path.parent / "outline.json").is_file() and (path.parent / "document.md").is_file()
    ]
    ledgers = find_ledgers(vault_root)
    written: list[str] = []
    errors: list[str] = []
    if not args.check:
        output_dir.mkdir(parents=True, exist_ok=True)
    for bundle in sorted(set(bundles)):
        try:
            projection = build_projection(bundle, vault_root, ledgers.get(bundle.name))
            target = output_dir / projection_name(bundle)
            if not args.check:
                target.write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(relative_path(target, vault_root))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{bundle}: {exc}")

    print(json.dumps({"status": "warn" if errors else "ok", "check": args.check, "projections": written, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
