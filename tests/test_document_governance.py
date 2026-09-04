from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap" / "scripts" / "init_obsidian_vault.py"
MANAGER = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "manage_document_governance.py"
LINT = ROOT / "hermes-obsidian-vault-lint" / "scripts" / "lint_vault.py"
INGEST = ROOT / "hermes-obsidian-controlled-ingest"


def create_engineering_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "EngineeringVault"
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(vault),
            "--profile",
            "engineering",
            "--vault-id",
            "vault-stage-two",
            "--vault-name",
            "Stage Two",
            "--security-domain",
            "internal-test",
        ],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    return vault


def run_manager(vault: Path, command: str, *arguments: str, expect: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, str(MANAGER), "--vault", str(vault), command, *arguments, "--json"],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == expect, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    return json.loads(completed.stdout) if completed.stdout else {"stderr": completed.stderr}


def organization_add(vault: Path, revision: int = 0, status: str = "candidate") -> dict:
    return run_manager(
        vault,
        "organization-add",
        "--organization-id",
        "organization-owner",
        "--name",
        "Owner Organization",
        "--alias",
        "Owner",
        "--status",
        status,
        "--expected-revision",
        str(revision),
        "--actor",
        "test-suite",
    )


def register_version(vault: Path, suffix: str, revision: int, content_hash: str | None = None) -> dict:
    return run_manager(
        vault,
        "register",
        "--document-id",
        "doc-shared-specification",
        "--version-id",
        f"version-shared-{suffix}",
        "--collection-id",
        "collection-engineering",
        "--title",
        "Shared specification",
        "--business-version",
        suffix,
        "--resource-id",
        f"resource-shared-{suffix}",
        "--storage-uri",
        f"local://10_Raw/source-{suffix}.pdf",
        "--content-sha256",
        content_hash or suffix[0] * 64,
        "--processing-status",
        "completed",
        "--authority-status",
        "official",
        "--source-occurrence-id",
        f"occurrence-shared-{suffix}",
        "--source-organization-id",
        "organization-owner",
        "--source-collection-id",
        "collection-owner-source",
        "--original-relative-path",
        f"incoming/source-{suffix}.pdf",
        "--received-at",
        "2026-09-04T08:00:00Z",
        "--created-at",
        "2026-09-04T08:00:00Z",
        "--expected-revision",
        str(revision),
        "--actor",
        "test-suite",
    )


def test_manager_registers_and_atomically_activates_versions(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    initial = run_manager(vault, "validate")
    assert initial["ok"] is True
    assert initial["registry_revision"] == 0

    assert organization_add(vault)["registry_revision"] == 1
    assert register_version(vault, "a", 0)["registry_revision"] == 1

    rejected = run_manager(
        vault,
        "activate",
        "--version-id",
        "version-shared-a",
        "--expected-revision",
        "1",
        "--actor",
        "test-suite",
        expect=2,
    )
    assert "approved" in rejected["stderr"]

    approved = run_manager(
        vault,
        "organization-status",
        "--organization-id",
        "organization-owner",
        "--status",
        "approved",
        "--expected-revision",
        "1",
        "--actor",
        "reviewer-one",
    )
    assert approved["registry_revision"] == 2

    first_activation = run_manager(
        vault,
        "activate",
        "--version-id",
        "version-shared-a",
        "--expected-revision",
        "1",
        "--actor",
        "reviewer-one",
    )
    assert first_activation["registry_revision"] == 2
    assert first_activation["superseded_version_id"] is None

    assert register_version(vault, "b", 2)["registry_revision"] == 3
    second_activation = run_manager(
        vault,
        "activate",
        "--version-id",
        "version-shared-b",
        "--expected-revision",
        "3",
        "--actor",
        "reviewer-one",
    )
    assert second_activation["registry_revision"] == 4
    assert second_activation["superseded_version_id"] == "version-shared-a"

    registry = json.loads(
        (vault / "_system" / "metadata" / "document-registry.json").read_text(encoding="utf-8")
    )
    by_version = {record["version_id"]: record for record in registry["records"]}
    assert by_version["version-shared-a"]["governance_status"] == "superseded"
    assert by_version["version-shared-b"]["governance_status"] == "active"
    assert by_version["version-shared-b"]["supersedes_version_id"] == "version-shared-a"
    assert len(registry["events"]) == 4
    assert {event["action"] for event in registry["events"]} == {
        "document_registered",
        "document_activated",
    }
    assert all(event["actor"] in {"test-suite", "reviewer-one"} for event in registry["events"])
    organizations = json.loads(
        (vault / "_system" / "metadata" / "source-organizations.json").read_text(encoding="utf-8")
    )
    assert [event["actor"] for event in organizations["events"]] == ["test-suite", "reviewer-one"]

    validated = run_manager(vault, "validate")
    assert validated["ok"] is True
    linted = subprocess.run(
        [
            sys.executable,
            str(LINT),
            "--vault",
            str(vault),
            "--profile",
            "post-ingest",
            "--ingest-skill-path",
            str(INGEST),
            "--json",
        ],
        text=True,
        capture_output=True,
    )
    assert linted.returncode == 0, linted.stderr
    assert json.loads(linted.stdout)["summary"]["errors"] == 0


def test_manager_deduplicates_content_and_appends_source_occurrence(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    register_version(vault, "a", 0)
    repeated = register_version(vault, "a", 1)
    assert repeated["changed"] is False
    assert repeated["registry_revision"] == 1

    duplicate = run_manager(
        vault,
        "register",
        "--document-id",
        "doc-other",
        "--version-id",
        "version-other-a",
        "--collection-id",
        "collection-engineering",
        "--title",
        "Duplicate",
        "--resource-id",
        "resource-other-a",
        "--storage-uri",
        "local://10_Raw/duplicate.pdf",
        "--content-sha256",
        "a" * 64,
        "--source-occurrence-id",
        "occurrence-other-a",
        "--source-organization-id",
        "organization-owner",
        "--source-collection-id",
        "collection-owner-source",
        "--original-relative-path",
        "incoming/duplicate.pdf",
        "--expected-revision",
        "1",
        "--actor",
        "test-suite",
        expect=2,
    )
    assert "use add-source" in duplicate["stderr"]

    added = run_manager(
        vault,
        "add-source",
        "--version-id",
        "version-shared-a",
        "--source-occurrence-id",
        "occurrence-second-delivery",
        "--source-organization-id",
        "organization-owner",
        "--source-collection-id",
        "collection-second-delivery",
        "--original-relative-path",
        "second/source-a.pdf",
        "--expected-revision",
        "1",
        "--actor",
        "test-suite",
    )
    assert added["registry_revision"] == 2
    registry = json.loads(
        (vault / "_system" / "metadata" / "document-registry.json").read_text(encoding="utf-8")
    )
    assert len(registry["records"]) == 1
    assert len(registry["records"][0]["source_occurrences"]) == 2


def test_revision_conflict_and_invalid_mutation_leave_registry_unchanged(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    register_version(vault, "a", 0)
    registry_path = vault / "_system" / "metadata" / "document-registry.json"
    original = registry_path.read_bytes()

    conflict = run_manager(
        vault,
        "status",
        "--version-id",
        "version-shared-a",
        "--processing-status",
        "failed",
        "--expected-revision",
        "0",
        "--actor",
        "stale-writer",
        expect=2,
    )
    assert "Revision conflict" in conflict["stderr"]
    assert registry_path.read_bytes() == original

    invalid = run_manager(
        vault,
        "add-source",
        "--version-id",
        "version-shared-a",
        "--source-occurrence-id",
        "occurrence-invalid-org",
        "--source-organization-id",
        "organization-missing",
        "--source-collection-id",
        "collection-owner-source",
        "--original-relative-path",
        "incoming/source.pdf",
        "--expected-revision",
        "1",
        "--actor",
        "test-suite",
        expect=2,
    )
    assert "unknown_organization" in invalid["stderr"]
    assert registry_path.read_bytes() == original
    assert not (vault / "_system" / "metadata" / ".document-governance.lock").exists()

    updated = run_manager(
        vault,
        "status",
        "--version-id",
        "version-shared-a",
        "--authority-status",
        "reference",
        "--expected-revision",
        "1",
        "--actor",
        "reviewer-one",
    )
    assert updated["changed"] is True
    assert updated["registry_revision"] == 2
    assert updated["changes"]["authority_status"] == {"from": "official", "to": "reference"}
