from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


SEARCHABLE_STATUSES = {"active", "superseded", "withdrawn"}


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def control_path(vault_root: Path, value: Any) -> Path:
    pure = PurePosixPath(str(value or "").replace("\\", "/"))
    if not pure.parts or pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
        raise ValueError("Invalid governance registry path")
    resolved = vault_root.joinpath(*pure.parts).resolve()
    resolved.relative_to(vault_root.resolve())
    return resolved


def vault_relative(path: Path, vault_root: Path) -> str | None:
    try:
        return path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return None


def eligible_corpus_paths(vault_root: Path, include_historical: bool = True) -> set[str] | None:
    """Return governed searchable paths, or ``None`` for a legacy Vault."""
    vault_root = vault_root.resolve()
    vault_manifest_path = vault_root / "_system" / "vault.json"
    if not vault_manifest_path.is_file():
        return None
    vault_manifest = load_object(vault_manifest_path)
    governance = vault_manifest.get("governance")
    if not isinstance(governance, dict) or governance.get("enabled") is not True:
        raise ValueError("Invalid enabled governance control plane")
    repository = governance.get("repository")
    if not isinstance(repository, dict) or repository.get("contract") != "hermes-governance/v1":
        raise ValueError("Unsupported governance repository contract")
    registry = load_object(control_path(vault_root, repository.get("registry_path")))
    if repository.get("backend") != "json" or registry.get("repository_contract") != "hermes-governance/v1":
        raise ValueError("Unsupported governance backend or registry")
    records = {item["version_id"]: item for item in registry["records"]}
    organizations = load_object(control_path(vault_root, governance.get("organizations_path")))
    approved = {item["id"] for item in organizations["organizations"] if item.get("status") == "approved"}
    def consistent(projection: Any, source: Any) -> bool:
        if not isinstance(projection, dict) or not isinstance(source, dict):
            return False
        record = records.get(projection.get("version_id"), {})
        return bool(record) and all(
            projection.get(key) == value for key, value in {
                "contract": "hermes-governance/v1", "vault_id": vault_manifest["vault"]["id"],
                "document_id": record.get("document_id"), "resource_id": record.get("resource_id"),
                "registry_path": repository["registry_path"],
            }.items()
        ) and source.get("sha256") == record.get("content_sha256") and type(
            projection.get("registry_revision")
        ) is int and 0 <= projection["registry_revision"] <= registry["registry_revision"]
    eligible_versions = {
        str(record.get("version_id"))
        for record in registry.get("records", [])
        if isinstance(record, dict)
        and record.get("processing_status") == "completed"
        and record.get("governance_status") in (SEARCHABLE_STATUSES if include_historical else {"active"})
        and record.get("source_occurrences")
        and all(item.get("source_organization_id") in approved for item in record["source_occurrences"])
    }

    eligible: set[str] = set()
    converted = vault_root / "10_Raw" / "converted"
    if converted.is_dir():
        for manifest_path in converted.rglob("manifest.json"):
            manifest = load_object(manifest_path)
            projection = manifest.get("governance")
            if not consistent(projection, manifest.get("source")) or projection.get("version_id") not in eligible_versions:
                continue
            document = manifest.get("document")
            relative = str(document.get("path") or "document.md") if isinstance(document, dict) else "document.md"
            document_path = vault_relative(manifest_path.parent / relative, vault_root)
            if document_path:
                eligible.add(document_path)

    reports = vault_root / "_system" / "reports"
    excluded_outputs: set[str] = set()
    if reports.is_dir():
        for ledger_path in reports.glob("*.section-ledger.json"):
            ledger = load_object(ledger_path)
            projection = ledger.get("governance")
            if not consistent(projection, ledger.get("source")) or projection.get("version_id") not in eligible_versions:
                for section in ledger.get("sections", []):
                    for output in section.get("outputs", []):
                        excluded_outputs.add(control_path(vault_root, output).relative_to(vault_root).as_posix())
                continue
            source_map = ledger_path.with_name(ledger_path.name.replace(".section-ledger.json", ".source-map.md"))
            if source_map.is_file():
                relative = vault_relative(source_map, vault_root)
                if relative:
                    eligible.add(relative)
            for section in ledger.get("sections", []):
                if not isinstance(section, dict):
                    continue
                for output in section.get("outputs", []):
                    output_path = control_path(vault_root, output)
                    if output_path.is_file():
                        relative = vault_relative(output_path, vault_root)
                        if relative:
                            eligible.add(relative)
    return eligible - excluded_outputs
