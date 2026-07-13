#!/usr/bin/env python3
"""Read-only governance lint for Hermes + Obsidian vaults."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
TOOL_NAME = "hermes-obsidian-vault-lint"
ALLOWED_PROFILES = {"post-ingest", "query-ready", "strict", "qa-review"}
ALLOWED_LEDGER_STATUSES = {"pending", "in_progress", "ingested", "qa_required", "skipped", "stale"}
REQUIRED_DIRS = [
    "10_Raw",
    "30_Cards",
    "40_Concepts",
    "50_Projects",
    "90_Dataview",
    "_system",
    "_system/metadata",
    "_system/reports",
]
GOVERNED_MARKDOWN_DIRS = ["30_Cards", "40_Concepts", "50_Projects", "_system/reports"]
SOURCE_MAP_SUFFIX = ".source-map.md"
LEDGER_SUFFIX = ".section-ledger.json"
BUNDLE_ID_PATTERN = re.compile(r"bundle-v2-[0-9a-fA-F]{16,64}")
QA_BOUNDARY_TERMS = (
    "needs-qa",
    "qa_required",
    "qa required",
    "manual qa",
    "人工 qa",
    "人工复核",
    "待复核",
    "需复核",
    "不提升为权威",
    "暂不提升为权威",
    "not authoritative",
    "do not promote",
)
HIGH_AUTHORITY_STATUSES = {"approved", "authoritative", "final", "published", "verified"}
HIGH_AUTHORITY_EVIDENCE_LEVELS = {"clear", "source-backed"}


@dataclass
class Issue:
    code: str
    severity: str
    path: str
    message: str
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }
        if self.hint:
            value["hint"] = self.hint
        if self.details:
            value["details"] = self.details
        return value


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def rel(path: Path, vault: Path) -> str:
    try:
        return path.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def from_wsl_path(value: str) -> str:
    if value.startswith("/mnt/") and len(value) > 6 and value[6] == "/":
        drive = value[5].upper()
        return f"{drive}:{value[6:]}".replace("/", "\\")
    return value


def resolve_vault_path(vault: Path, value: str | None) -> Path | None:
    if not value:
        return None
    converted = from_wsl_path(str(value))
    path = Path(converted)
    if path.is_absolute():
        return path
    return vault / path


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[1:-1]
    return value


def parse_frontmatter(path: Path) -> dict[str, Any] | None:
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        return None

    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw in lines[1:end]:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current_list_key and stripped.startswith("- "):
            data.setdefault(current_list_key, []).append(strip_quotes(stripped[2:]))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            data[key] = []
            current_list_key = key
        elif value == "[]":
            data[key] = []
        else:
            data[key] = strip_quotes(value)
    return data


def markdown_body(path: Path) -> str:
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return "\n".join(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :])
    return ""


def split_table_row(line: str) -> list[str]:
    value = line.strip().strip("|")
    return [cell.strip() for cell in value.split("|")]


def parse_markdown_tables(body: str) -> list[dict[str, Any]]:
    lines = body.splitlines()
    tables: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(lines):
        header_line = lines[index]
        separator_line = lines[index + 1]
        if "|" not in header_line or "|" not in separator_line:
            index += 1
            continue
        headers = split_table_row(header_line)
        separators = split_table_row(separator_line)
        if len(headers) != len(separators) or not separators or not all(
            re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separators
        ):
            index += 1
            continue
        rows: list[list[str]] = []
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            row = split_table_row(lines[index])
            if len(row) == len(headers):
                rows.append(row)
            index += 1
        tables.append({"headers": headers, "rows": rows})
    return tables


def normalized_header(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.strip().strip("`").casefold())


def structured_evidence_rows(body: str) -> list[dict[str, str]]:
    aliases = {
        "bundle": {"bundle", "bundle id", "source bundle id"},
        "section": {"section", "section id", "source section id"},
        "pages": {"pages", "source pages"},
        "lines": {"lines", "owned lines", "source lines"},
    }
    evidence: list[dict[str, str]] = []
    for table in parse_markdown_tables(body):
        normalized = [normalized_header(item) for item in table["headers"]]
        indexes: dict[str, int] = {}
        for field_name, names in aliases.items():
            found = next((i for i, header in enumerate(normalized) if header in names), None)
            if found is not None:
                indexes[field_name] = found
        if set(indexes) != set(aliases):
            continue
        for row in table["rows"]:
            values = {name: row[position].strip().strip("`") for name, position in indexes.items()}
            match = BUNDLE_ID_PATTERN.search(values["bundle"])
            if match:
                values["bundle"] = match.group(0)
                values["section"] = values["section"].strip().strip("`")
                evidence.append(values)
    return evidence


def severity_for_open_qa(profile: str) -> str:
    if profile == "strict":
        return "error"
    if profile == "qa-review":
        return "info"
    return "warning"


def severity_for_pending(profile: str) -> str:
    return "error" if profile == "strict" else "warning"


def discover_ingest_skill(script_path: Path, supplied: str | None) -> Path | None:
    if supplied:
        path = Path(supplied).expanduser()
        return path.resolve() if path.is_dir() else path
    sibling = script_path.resolve().parents[2] / "hermes-obsidian-controlled-ingest"
    if sibling.is_dir():
        return sibling
    return None


def load_bundle_validator(ingest_skill_path: Path | None) -> Any | None:
    if ingest_skill_path is None:
        return None
    validator_path = ingest_skill_path / "scripts" / "validate_document_bundle.py"
    if not validator_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("hermes_bundle_validator", validator_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_issue(issues: list[Issue], code: str, severity: str, path: str, message: str, hint: str = "", **details: Any) -> None:
    issues.append(Issue(code, severity, path, message, hint, {k: v for k, v in details.items() if v is not None}))


def lint_structure(vault: Path, issues: list[Issue], metrics: dict[str, Any]) -> None:
    if not vault.exists():
        add_issue(issues, "vault.missing_path", "error", str(vault), "Vault path does not exist.")
        return
    if not vault.is_dir():
        add_issue(issues, "vault.missing_path", "error", str(vault), "Vault path is not a directory.")
        return
    for item in REQUIRED_DIRS:
        target = vault / item
        if not target.is_dir():
            add_issue(issues, "vault.missing_required_dir", "error", item, f"Required vault directory is missing: {item}")
    for item in ("AGENTS.md", "_system/metadata/concept-registry.md"):
        target = vault / item
        if not target.is_file():
            add_issue(issues, "vault.missing_governance_file", "warning", item, f"Governance file is missing: {item}")
    metrics["required_dirs_checked"] = len(REQUIRED_DIRS)


def find_bundles(vault: Path) -> list[Path]:
    converted = vault / "10_Raw" / "converted"
    if not converted.is_dir():
        return []
    return sorted(
        path
        for path in converted.iterdir()
        if path.is_dir() and (path.name.endswith("_document_bundle") or path.name.endswith("_image_document_bundle"))
    )


def lint_bundles(
    vault: Path,
    validator: Any | None,
    issues: list[Issue],
    metrics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    bundles = find_bundles(vault)
    metrics["bundles"] = len(bundles)
    results: dict[str, dict[str, Any]] = {}
    if bundles and validator is None:
        add_issue(
            issues,
            "bundle.validator_unavailable",
            "warning",
            "10_Raw/converted",
            "Bundle directories exist but the controlled-ingest bundle validator was not found.",
            "Pass --ingest-skill-path or place this skill next to hermes-obsidian-controlled-ingest.",
        )
        return results
    for bundle in bundles:
        missing = [name for name in ("manifest.json", "document.md", "outline.json") if not (bundle / name).is_file()]
        if missing:
            add_issue(
                issues,
                "bundle.missing_control_file",
                "error",
                rel(bundle, vault),
                "Bundle is missing required control files.",
                "Rebuild or recover this derived bundle; do not modify 10_Raw originals.",
                missing=missing,
            )
            continue
        try:
            result = validator.validate_bundle(bundle.resolve()) if validator is not None else {}
        except Exception as exc:  # pragma: no cover - defensive boundary
            add_issue(issues, "bundle.validation_failed", "error", rel(bundle, vault), f"Bundle validator crashed: {exc}")
            continue
        status = str(result.get("status", "unknown"))
        results[str(bundle.resolve())] = result
        if status == "fail":
            add_issue(
                issues,
                "bundle.validation_failed",
                "error",
                rel(bundle, vault),
                "Bundle validation returned fail.",
                "Use controlled ingest recovery rules before downstream writes.",
                validator_issues=result.get("issues", []),
            )
        elif status == "warn":
            validator_issues = result.get("issues", [])
            add_issue(
                issues,
                "bundle.validation_warning",
                "warning",
                rel(bundle, vault),
                "Bundle validation returned warn.",
                "Keep warning-affected formulas, tables, figures, and parameters under QA until reviewed.",
                validator_issue_count=len(validator_issues) if isinstance(validator_issues, list) else None,
                validator_issue_codes=sorted({str(item.get("code")) for item in validator_issues if isinstance(item, dict)}) if isinstance(validator_issues, list) else None,
                review_required=result.get("review_required", []),
            )
    return results


def load_ledgers(vault: Path, issues: list[Issue], metrics: dict[str, Any]) -> list[dict[str, Any]]:
    reports = vault / "_system" / "reports"
    ledger_paths = sorted(reports.glob(f"*{LEDGER_SUFFIX}")) if reports.is_dir() else []
    ledgers: list[dict[str, Any]] = []
    status_totals: Counter[str] = Counter()
    for path in ledger_paths:
        try:
            value = json.loads(read_text(path))
        except Exception as exc:
            add_issue(issues, "ledger.invalid_json", "error", rel(path, vault), f"Cannot parse section ledger JSON: {exc}")
            continue
        sections = value.get("sections", [])
        if not isinstance(sections, list):
            add_issue(issues, "ledger.invalid_json", "error", rel(path, vault), "Ledger sections must be a list.")
            sections = []
        counts = Counter(str(section.get("status")) for section in sections if isinstance(section, dict))
        status_totals.update(counts)
        ledgers.append({"path": path, "value": value, "counts": counts})
    metrics["ledgers"] = len(ledgers)
    for status in sorted(ALLOWED_LEDGER_STATUSES):
        metrics[f"sections_{status}"] = int(status_totals.get(status, 0))
    return ledgers


def lint_ledgers(vault: Path, profile: str, ledgers: list[dict[str, Any]], issues: list[Issue], metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_bundle_id: dict[str, dict[str, Any]] = {}
    for item in ledgers:
        path: Path = item["path"]
        ledger = item["value"]
        counts: Counter[str] = item["counts"]
        bundle_id = str(ledger.get("bundle_id", ""))
        if bundle_id:
            by_bundle_id[bundle_id] = item

        unknown = sorted(status for status in counts if status not in ALLOWED_LEDGER_STATUSES)
        if unknown:
            add_issue(issues, "ledger.unknown_status", "error", rel(path, vault), "Ledger contains unknown section statuses.", unknown_statuses=unknown)
        if counts.get("in_progress", 0):
            add_issue(issues, "ledger.in_progress", "error", rel(path, vault), f"{counts['in_progress']} sections are still in_progress.")
        if counts.get("stale", 0):
            add_issue(issues, "ledger.stale", "error", rel(path, vault), f"{counts['stale']} sections are stale.")
        if counts.get("pending", 0):
            add_issue(issues, "ledger.pending", severity_for_pending(profile), rel(path, vault), f"{counts['pending']} sections are pending.")
        if counts.get("qa_required", 0):
            add_issue(
                issues,
                "ledger.qa_open",
                severity_for_open_qa(profile),
                rel(path, vault),
                f"{counts['qa_required']} sections are qa_required.",
                "Keep as controlled QA or run targeted source-page/asset review before promotion.",
            )

        for section in ledger.get("sections", []):
            if not isinstance(section, dict):
                continue
            for output in section.get("outputs", []) if isinstance(section.get("outputs"), list) else []:
                target = resolve_vault_path(vault, str(output))
                if target is None or not target.is_file():
                    add_issue(
                        issues,
                        "ledger.output_missing",
                        "error",
                        rel(path, vault),
                        "Ledger records an output path that does not exist.",
                        "Either restore the governed output or update the ledger through controlled tooling.",
                        section_id=section.get("id"),
                        output=output,
                    )
    metrics["ledger_bundle_ids"] = len(by_bundle_id)
    return by_bundle_id


def lint_source_maps(vault: Path, ledgers_by_bundle: dict[str, dict[str, Any]], issues: list[Issue], metrics: dict[str, Any]) -> None:
    reports = vault / "_system" / "reports"
    source_maps = sorted(reports.glob(f"*{SOURCE_MAP_SUFFIX}")) if reports.is_dir() else []
    metrics["source_maps"] = len(source_maps)
    maps_by_bundle: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in source_maps:
        frontmatter = parse_frontmatter(path)
        if frontmatter is None:
            add_issue(issues, "frontmatter.missing", "error", rel(path, vault), "Source map is missing frontmatter.")
            continue
        bundle_id = str(frontmatter.get("bundle_id", ""))
        if bundle_id:
            maps_by_bundle[bundle_id] = (path, frontmatter)

    for bundle_id, item in ledgers_by_bundle.items():
        ledger = item["value"]
        ledger_path: Path = item["path"]
        found = maps_by_bundle.get(bundle_id)
        if found is None:
            add_issue(issues, "source_map.missing", "error", rel(ledger_path, vault), "No source map found for ledger bundle_id.", bundle_id=bundle_id)
            continue
        map_path, frontmatter = found
        checks = {
            "ledger_revision": str(ledger.get("revision")),
            "ingest_state": str(ledger.get("state")),
            "validation_status": str((ledger.get("validation") or {}).get("status")),
            "source_sha256": str((ledger.get("source") or {}).get("sha256")),
        }
        for key, expected in checks.items():
            actual = str(frontmatter.get(key, ""))
            if actual != expected:
                add_issue(
                    issues,
                    "source_map.mismatch",
                    "error",
                    rel(map_path, vault),
                    f"Source map frontmatter {key} does not match ledger.",
                    key=key,
                    expected=expected,
                    actual=actual,
                    ledger=rel(ledger_path, vault),
                )


def iter_governed_markdown(vault: Path) -> list[Path]:
    paths: list[Path] = []
    for folder in GOVERNED_MARKDOWN_DIRS:
        root = vault / folder
        if not root.is_dir():
            continue
        paths.extend(path for path in root.rglob("*.md") if path.name.upper() != "README.md")
    return sorted(paths)


def section_lookup(ledgers_by_bundle: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for bundle_id, item in ledgers_by_bundle.items():
        for section in item["value"].get("sections", []):
            if isinstance(section, dict) and section.get("id"):
                lookup[(bundle_id, str(section.get("id")))] = section
    return lookup


def semantic_warning_severity(profile: str) -> str:
    return "error" if profile == "strict" else "warning"


def section_needs_qa(section: dict[str, Any]) -> bool:
    return str(section.get("status", "")).casefold() == "qa_required" or str(section.get("quality", "")).casefold() in {
        "warn",
        "fail",
    }


def has_qa_boundary(frontmatter: dict[str, Any], body: str) -> bool:
    if str(frontmatter.get("evidence_level", "")).casefold() == "needs-qa":
        return True
    lowered = body.casefold()
    return any(term in lowered for term in QA_BOUNDARY_TERMS)


def lint_semantic_boundaries(
    relative: str,
    profile: str,
    frontmatter: dict[str, Any],
    body: str,
    bundle_ids: list[str],
    ledgers_by_bundle: dict[str, dict[str, Any]],
    known_sections: dict[tuple[str, str], dict[str, Any]],
    issues: list[Issue],
    metrics: dict[str, Any],
) -> None:
    body_bundle_ids = sorted(set(BUNDLE_ID_PATTERN.findall(body)))
    all_bundle_ids = sorted(set(bundle_ids) | set(body_bundle_ids))
    rows = structured_evidence_rows(body)
    affected: list[dict[str, str]] = []

    if len(all_bundle_ids) > 1:
        metrics["multi_source_artifacts"] = int(metrics.get("multi_source_artifacts", 0)) + 1
        if rows:
            metrics["structured_multi_source_artifacts"] = int(metrics.get("structured_multi_source_artifacts", 0)) + 1
        else:
            add_issue(
                issues,
                "synthesis.multi_source_unstructured",
                semantic_warning_severity(profile),
                relative,
                "Multi-source artifact has no structured evidence table with bundle, section, pages, and owned/source lines.",
                "Add a row-level evidence table so each synthesized claim can be traced to a governed section.",
                bundle_count=len(all_bundle_ids),
            )

        unknown = [bundle_id for bundle_id in all_bundle_ids if bundle_id not in ledgers_by_bundle]
        if unknown:
            add_issue(
                issues,
                "evidence.unknown_bundle",
                "error",
                relative,
                "Multi-source artifact references bundle ids with no matching ledger.",
                bundle_ids=unknown,
            )

    for row in rows:
        section = known_sections.get((row["bundle"], row["section"]))
        if row["bundle"] not in ledgers_by_bundle:
            continue
        if section is None:
            add_issue(
                issues,
                "evidence.unknown_section",
                "error",
                relative,
                "Structured evidence row references a section not present in the matching ledger.",
                bundle_id=row["bundle"],
                section_id=row["section"],
            )
        elif section_needs_qa(section):
            affected.append({"bundle_id": row["bundle"], "section_id": row["section"]})

    if len(bundle_ids) == 1:
        section_id = str(frontmatter.get("source_section_id", "")).strip().strip('"')
        section = known_sections.get((bundle_ids[0], section_id))
        if section is not None and section_needs_qa(section):
            affected.append({"bundle_id": bundle_ids[0], "section_id": section_id})

    if not affected:
        return

    unique_affected = list({(item["bundle_id"], item["section_id"]): item for item in affected}.values())
    metrics["qa_affected_artifacts"] = int(metrics.get("qa_affected_artifacts", 0)) + 1
    artifact_status = str(frontmatter.get("status", "")).casefold()
    evidence_level = str(frontmatter.get("evidence_level", "")).casefold()
    if artifact_status in HIGH_AUTHORITY_STATUSES or evidence_level in HIGH_AUTHORITY_EVIDENCE_LEVELS:
        add_issue(
            issues,
            "qa.authority_overpromoted",
            "error",
            relative,
            "Artifact promotes QA-affected evidence with an authoritative status or evidence level.",
            "Downgrade the artifact/evidence level to needs-qa or complete targeted source-page and asset review first.",
            artifact_status=artifact_status or None,
            evidence_level=evidence_level or None,
            affected_sections=unique_affected,
        )
    elif not has_qa_boundary(frontmatter, body):
        add_issue(
            issues,
            "qa.boundary_weak",
            semantic_warning_severity(profile),
            relative,
            "Artifact uses QA-affected evidence without an explicit needs-qa boundary.",
            "Add evidence_level: needs-qa or state the targeted page/table/figure review still required.",
            affected_sections=unique_affected,
        )


def lint_markdown_artifacts(
    vault: Path,
    profile: str,
    ledgers_by_bundle: dict[str, dict[str, Any]],
    issues: list[Issue],
    metrics: dict[str, Any],
) -> None:
    paths = iter_governed_markdown(vault)
    metrics["governed_markdown_files"] = len(paths)
    known_sections = section_lookup(ledgers_by_bundle)

    for path in paths:
        if path.name.endswith(SOURCE_MAP_SUFFIX):
            continue
        frontmatter = parse_frontmatter(path)
        body = markdown_body(path)
        relative = rel(path, vault)
        if frontmatter is None:
            add_issue(issues, "frontmatter.missing", "warning", relative, "Governed Markdown file is missing frontmatter.")
            continue
        for field_name in ("type", "status"):
            if not frontmatter.get(field_name):
                add_issue(issues, "frontmatter.missing_field", "warning", relative, f"Frontmatter field is missing: {field_name}")

        artifact_type = str(frontmatter.get("type", ""))
        source_bundle_value = frontmatter.get("source_bundle_id", "")
        if isinstance(source_bundle_value, list):
            bundle_ids = [str(item).strip() for item in source_bundle_value if str(item).strip()]
        else:
            bundle_ids = [str(source_bundle_value).strip()] if str(source_bundle_value).strip() else []
        if artifact_type == "report" and path.name.endswith(".controlled-ingest-log.md"):
            continue

        lint_semantic_boundaries(
            relative,
            profile,
            frontmatter,
            body,
            bundle_ids,
            ledgers_by_bundle,
            known_sections,
            issues,
            metrics,
        )

        if not bundle_ids:
            continue

        if len(bundle_ids) > 1:
            continue

        required = ["source_sha256", "source_section_id", "source_lines", "source_pages", "source_assets"]
        section_id = str(frontmatter.get("source_section_id", "")).strip().strip('"')
        document_level = section_id in {"multiple-pass-sections", "multiple-sections"}
        for field_name in required:
            if document_level and field_name in {"source_pages", "source_assets"}:
                continue
            value = frontmatter.get(field_name)
            if value in (None, "", []):
                severity = "error" if profile in {"query-ready", "strict"} and artifact_type in {"knowledge-card", "spec-index"} else "warning"
                add_issue(issues, "evidence.missing_field", severity, relative, f"Citation contract field is empty: {field_name}")

        source_bundle_id = bundle_ids[0]
        ledger_item = ledgers_by_bundle.get(source_bundle_id)
        if ledger_item is None:
            add_issue(issues, "evidence.unknown_bundle", "error", relative, "Artifact references a source_bundle_id with no matching ledger.", bundle_id=source_bundle_id)
            continue
        source_sha = str(frontmatter.get("source_sha256", ""))
        expected_sha = str((ledger_item["value"].get("source") or {}).get("sha256", ""))
        if source_sha and expected_sha and source_sha != expected_sha:
            add_issue(issues, "evidence.unknown_bundle", "error", relative, "Artifact source_sha256 does not match the matching ledger.", expected=expected_sha, actual=source_sha)

        if section_id and not document_level:
            section = known_sections.get((source_bundle_id, section_id))
            if section is None:
                add_issue(issues, "evidence.unknown_section", "error", relative, "Artifact references a section id not present in the matching ledger.", section_id=section_id)
            else:
                outputs = [str(item).replace("\\", "/") for item in section.get("outputs", []) if isinstance(item, str)]
                normalized_rel = relative.replace("\\", "/")
                if outputs and normalized_rel not in outputs:
                    add_issue(
                        issues,
                        "evidence.not_recorded_in_ledger",
                        "warning",
                        relative,
                        "Artifact cites a ledger section, but that section does not record this file as an output.",
                        section_id=section_id,
                        ledger_outputs=outputs,
                    )


def build_markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "---",
        "type: report",
        "status: draft",
        "created:",
        "domains:",
        "  - controlled-lint",
        "---",
        "",
        "# Hermes Obsidian Vault Lint Report",
        "",
        f"- Vault: `{result['vault']}`",
        f"- Profile: `{result['profile']}`",
        f"- Status: `{result['status']}`",
        f"- Errors: `{result['summary']['errors']}`",
        f"- Warnings: `{result['summary']['warnings']}`",
        f"- Info: `{result['summary']['info']}`",
        "",
        "## Metrics",
        "",
    ]
    for key, value in sorted(result.get("metrics", {}).items()):
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Issues", ""])
    if not result.get("issues"):
        lines.append("No issues found.")
    for issue in result.get("issues", []):
        lines.append(f"- `{issue['severity']}` `{issue['code']}` `{issue['path']}`: {issue['message']}")
        if issue.get("hint"):
            lines.append(f"  - Hint: {issue['hint']}")
    lines.append("")
    return "\n".join(lines)


def lint(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    vault = Path(args.vault).expanduser().resolve()
    profile = args.profile
    issues: list[Issue] = []
    metrics: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []

    def stage(name: str, fn: Any) -> Any:
        before = time.perf_counter()
        value = fn()
        trace.append({"stage": name, "elapsed_ms": round((time.perf_counter() - before) * 1000, 2)})
        return value

    stage("structure", lambda: lint_structure(vault, issues, metrics))
    ingest_skill = discover_ingest_skill(Path(__file__), args.ingest_skill_path)
    validator = load_bundle_validator(ingest_skill)
    stage("bundles", lambda: lint_bundles(vault, validator, issues, metrics))
    ledgers = stage("ledgers.load", lambda: load_ledgers(vault, issues, metrics))
    ledgers_by_bundle = stage("ledgers.rules", lambda: lint_ledgers(vault, profile, ledgers, issues, metrics))
    stage("source_maps", lambda: lint_source_maps(vault, ledgers_by_bundle, issues, metrics))
    stage("artifacts", lambda: lint_markdown_artifacts(vault, profile, ledgers_by_bundle, issues, metrics))

    summary_counter = Counter(issue.severity for issue in issues)
    errors = int(summary_counter.get("error", 0))
    warnings = int(summary_counter.get("warning", 0))
    info = int(summary_counter.get("info", 0))
    status = "fail" if errors else "pass-with-warnings" if warnings else "pass"
    result = {
        "tool": TOOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "ok": errors == 0,
        "status": status,
        "profile": profile,
        "vault": str(vault),
        "summary": {"errors": errors, "warnings": warnings, "info": info},
        "metrics": metrics,
        "issues": [issue.to_dict() for issue in issues],
        "trace": trace if args.trace else [],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Hermes + Obsidian vault governance lint")
    parser.add_argument("--vault", required=True, help="Path to the governed Obsidian vault")
    parser.add_argument("--profile", choices=sorted(ALLOWED_PROFILES), default="post-ingest")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    parser.add_argument("--markdown-report", type=Path, help="Optional path for a persisted Markdown lint report")
    parser.add_argument("--fail-on", choices=["error", "warning"], default="error")
    parser.add_argument("--ingest-skill-path", help="Path to hermes-obsidian-controlled-ingest for bundle validation")
    parser.add_argument("--trace", action="store_true", help="Include stage timings in JSON")
    args = parser.parse_args()

    try:
        result = lint(args)
    except Exception as exc:  # pragma: no cover - final safety net
        result = {
            "tool": TOOL_NAME,
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "internal-error",
            "profile": args.profile,
            "vault": str(Path(args.vault).expanduser()),
            "summary": {"errors": 1, "warnings": 0, "info": 0},
            "metrics": {},
            "issues": [
                Issue(
                    "lint.internal_error",
                    "error",
                    str(Path(args.vault).expanduser()),
                    f"Lint crashed: {exc}",
                ).to_dict()
            ],
            "trace": [],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    if args.markdown_report:
        args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_report.write_text(build_markdown_report(result), encoding="utf-8", newline="\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["summary"]
        print(f"Status: {result['status']}")
        print(f"Errors: {summary['errors']}  Warnings: {summary['warnings']}  Info: {summary['info']}")
        for issue in result["issues"]:
            print(f"- [{issue['severity']}] {issue['code']} {issue['path']}: {issue['message']}")

    if result["summary"]["errors"]:
        return 2
    if args.fail_on == "warning" and result["summary"]["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
