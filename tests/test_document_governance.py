from __future__ import annotations

import json
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap" / "scripts" / "init_obsidian_vault.py"
MANAGER = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "manage_document_governance.py"
LINT = ROOT / "hermes-obsidian-vault-lint" / "scripts" / "lint_vault.py"
INGEST = ROOT / "hermes-obsidian-controlled-ingest"
BUNDLE_MANAGER = INGEST / "scripts" / "manage_bundle_ingest.py"
VALIDATOR = INGEST / "scripts" / "validate_document_bundle.py"


def test_stage_four_current_history_and_identity_gates(tmp_path: Path) -> None:
    locator = ROOT / "hermes-obsidian-controlled-query/scripts/locate_source_sections.py"
    spec = importlib.util.spec_from_file_location("governance_locator_test", locator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path.insert(0, str(ROOT / "qmd-like-rag/src"))
    from qmd_like_rag.corpus import resolve_sources

    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    raw = vault / "10_Raw/stage-three.pdf"
    raw.write_bytes(b"stage four original")
    ingest_start(vault, raw)
    bundle = create_bundle(vault, raw)
    run_manager(vault, "ingest-finish", "--bundle", str(bundle),
                "--version-id", "version-stage-three-1", "--expected-revision", "1", "--actor", "test")
    relative = (bundle / "document.md").relative_to(vault).as_posix()
    assert not module.GovernanceQueryPolicy(vault).allows(relative)
    assert resolve_sources(vault, ["10_Raw/converted/**/document.md"]) == []
    run_manager(vault, "activate", "--version-id", "version-stage-three-1",
                "--expected-revision", "2", "--actor", "reviewer")
    assert module.GovernanceQueryPolicy(vault).allows(relative)
    assert len(resolve_sources(vault, ["10_Raw/converted/**/document.md"])) == 1
    run_manager(vault, "status", "--version-id", "version-stage-three-1",
                "--governance-status", "withdrawn", "--expected-revision", "3", "--actor", "reviewer")
    assert not module.GovernanceQueryPolicy(vault).allows(relative)
    assert module.GovernanceQueryPolicy(vault, True).allows(relative)
    # Provider retains history; ordinary Query filters it using current state.
    assert len(resolve_sources(vault, ["10_Raw/converted/**/document.md"])) == 1
    path = bundle / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["governance"]["vault_id"] = "vault-wrong"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert not module.GovernanceQueryPolicy(vault, True).allows(relative)
    assert resolve_sources(vault, ["10_Raw/converted/**/document.md"]) == []
    linted = subprocess.run([sys.executable, str(LINT), "--vault", str(vault), "--json"],
                            text=True, capture_output=True)
    assert "governance.projection_mismatch" in {i["code"] for i in json.loads(linted.stdout)["issues"]}
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    assert module.GovernanceQueryPolicy(legacy).allows("30_Cards/existing.md")


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


def create_bundle(vault: Path, source: Path, source_hash: str | None = None) -> Path:
    bundle = vault / "10_Raw" / "converted" / "sample_document_bundle"
    bundle.mkdir(parents=True)
    document = "<!-- source-page: 1 -->\n# Sample\nEvidence\n"
    (bundle / "document.md").write_text(document, encoding="utf-8")
    outline = {
        "schema_version": "2.0",
        "document": "document.md",
        "sections": [
            {
                "id": "sample",
                "title": "Sample",
                "level": 1,
                "parent": None,
                "path": ["sample"],
                "start_line": 2,
                "end_line": 3,
                "pages": [1],
                "assets": [],
                "quality": "pass",
            }
        ],
    }
    (bundle / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    manifest = {
        "schema_version": "2.0",
        "profile": "engineering",
        "source": {
            "path": str(source),
            "filename": source.name,
            "sha256": source_hash or hashlib.sha256(source.read_bytes()).hexdigest(),
            "parsed_pages": 1,
        },
        "conversion": {"engine": "test", "backend": "fixture"},
        "document": {"path": "document.md", "line_count": 3},
        "outline": {"path": "outline.json", "section_count": 1},
        "images": [],
        "tables": [],
        "evidence": {"default_ingest": False, "files": [], "blocks": None},
        "quality": {"status": "pass", "issues": [], "review_required": []},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def ingest_start(vault: Path, source: Path, revision: int = 0) -> dict:
    return run_manager(
        vault,
        "ingest-start",
        "--raw-source",
        str(source),
        "--document-id",
        "doc-stage-three",
        "--version-id",
        "version-stage-three-1",
        "--collection-id",
        "collection-engineering",
        "--title",
        "Stage three document",
        "--business-version",
        "1",
        "--resource-id",
        "resource-stage-three-1",
        "--authority-status",
        "official",
        "--source-occurrence-id",
        "occurrence-stage-three-1",
        "--source-organization-id",
        "organization-owner",
        "--source-collection-id",
        "collection-owner-source",
        "--original-relative-path",
        "incoming/stage-three.pdf",
        "--expected-revision",
        str(revision),
        "--actor",
        "ingest-service",
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


def test_stage_three_lifecycle_projects_bundle_and_allows_ledger(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    raw = vault / "10_Raw" / "stage-three.pdf"
    raw.write_bytes(b"immutable source bytes")
    started = ingest_start(vault, raw)
    assert started["registry_revision"] == 1
    assert started["content_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()

    bundle = create_bundle(vault, raw)
    reports = vault / "_system" / "reports"
    before_finish = subprocess.run(
        [sys.executable, str(BUNDLE_MANAGER), "init", str(bundle), "--reports-dir", str(reports)],
        text=True,
        capture_output=True,
    )
    assert before_finish.returncode == 2
    assert "run governance ingest-finish first" in before_finish.stderr

    finished = run_manager(
        vault,
        "ingest-finish",
        "--bundle",
        str(bundle),
        "--version-id",
        "version-stage-three-1",
        "--expected-revision",
        "1",
        "--actor",
        "ingest-service",
    )
    assert finished["processing_status"] == "completed"
    assert finished["registry_revision"] == 2
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["governance"] == {
        "contract": "hermes-governance/v1",
        "vault_id": "vault-stage-two",
        "document_id": "doc-stage-three",
        "version_id": "version-stage-three-1",
        "resource_id": "resource-stage-three-1",
        "registry_path": "_system/metadata/document-registry.json",
        "registry_revision": 2,
    }

    ledger_result = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_MANAGER),
            "init",
            str(bundle),
            "--reports-dir",
            str(reports),
            "--json",
        ],
        text=True,
        capture_output=True,
    )
    assert ledger_result.returncode == 0, ledger_result.stderr
    ledger_path = Path(json.loads(ledger_result.stdout)["ledger"])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["governance"]["version_id"] == "version-stage-three-1"
    assert ledger["governance"]["processing_status"] == "completed"


def test_ingest_finish_hash_mismatch_preserves_registry_and_manifest(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    raw = vault / "10_Raw" / "stage-three.pdf"
    raw.write_bytes(b"immutable source bytes")
    ingest_start(vault, raw)
    bundle = create_bundle(vault, raw, source_hash="f" * 64)
    registry_path = vault / "_system" / "metadata" / "document-registry.json"
    manifest_path = bundle / "manifest.json"
    before_registry = registry_path.read_bytes()
    before_manifest = manifest_path.read_bytes()

    failed = run_manager(
        vault,
        "ingest-finish",
        "--bundle",
        str(bundle),
        "--version-id",
        "version-stage-three-1",
        "--expected-revision",
        "1",
        "--actor",
        "ingest-service",
        expect=2,
    )
    assert "source hash" in failed["stderr"]
    assert registry_path.read_bytes() == before_registry
    assert manifest_path.read_bytes() == before_manifest


def test_bundle_validator_rejects_malformed_optional_governance_projection(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    raw = vault / "10_Raw" / "stage-three.pdf"
    raw.write_bytes(b"source")
    bundle = create_bundle(vault, raw)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"] = {"contract": "wrong", "registry_revision": -1}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR), str(bundle), "--json"],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 2
    codes = {item["code"] for item in json.loads(completed.stdout)["issues"]}
    assert "incomplete-governance-projection" in codes
    assert "invalid-governance-contract" in codes
    assert "invalid-governance-revision" in codes


def test_failed_bundle_records_failed_processing_and_creates_only_blocked_ledger(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault, status="approved")
    raw = vault / "10_Raw" / "stage-three.pdf"
    raw.write_bytes(b"immutable source bytes")
    ingest_start(vault, raw)
    bundle = create_bundle(vault, raw)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence"]["default_ingest"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    finished = run_manager(
        vault,
        "ingest-finish",
        "--bundle",
        str(bundle),
        "--version-id",
        "version-stage-three-1",
        "--expected-revision",
        "1",
        "--actor",
        "ingest-service",
    )
    assert finished["validation_status"] == "fail"
    assert finished["processing_status"] == "failed"

    ledger_result = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_MANAGER),
            "init",
            str(bundle),
            "--reports-dir",
            str(vault / "_system" / "reports"),
            "--json",
        ],
        text=True,
        capture_output=True,
    )
    assert ledger_result.returncode == 0, ledger_result.stderr
    assert json.loads(ledger_result.stdout)["state"] == "blocked"


def test_candidate_source_can_build_draft_without_becoming_query_visible(tmp_path: Path) -> None:
    vault = create_engineering_vault(tmp_path)
    organization_add(vault)  # Candidate, not approved.
    raw = vault / "10_Raw/stage-three.pdf"
    raw.write_bytes(b"source for attributed draft")
    ingest_start(vault, raw)
    bundle = create_bundle(vault, raw)
    run_manager(vault, "ingest-finish", "--bundle", str(bundle), "--version-id", "version-stage-three-1",
                "--expected-revision", "1", "--actor", "test")

    def ledger_command(*args):
        result = subprocess.run([sys.executable, str(BUNDLE_MANAGER), *args, "--json"],
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    prepared = ledger_command("init", str(bundle), "--reports-dir", str(vault / "_system/reports"))
    ledger_path = Path(prepared["ledger"])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    section = next(s for s in ledger["sections"] if s["content_ranges"])
    ledger_command("update", str(ledger_path), "--section", section["id"], "--status", "in_progress",
                   "--expected-revision", str(ledger["revision"]))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    source = bundle / "document.md"
    evidence = {"path": source.relative_to(vault).as_posix(),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "lines": [section["content_ranges"][0]["start_line"], section["content_ranges"][0]["end_line"]],
                "qa": "usable"}
    output = "30_Cards/attributed-draft.md"
    data = {"contract": "hermes-knowledge-build/v2", "scope": "Fixture source section",
            "execution_status": "in_progress", "inspected_ranges": [dict(
                evidence, reason="Inspected source statement", ledger_path=ledger_path.relative_to(vault).as_posix(),
                ledger_revision=ledger["revision"], bundle_id=ledger["bundle_id"], section_id=section["id"])],
            "candidates": [{"id": "draft-001", "name": "Attributed draft", "kind": "fact",
                            "identity_rationale": "No existing page", "existing_targets": [],
                            "decision": "create", "reason": "Source statement supports a draft",
                            "outputs": [output], "evidence": [evidence]}]}
    record = vault / "_system/reports/draft.knowledge-build.json"

    def validate(phase):
        record.write_text(json.dumps(data), encoding="utf-8")
        result = subprocess.run([sys.executable, str(INGEST / "scripts/validate_knowledge_build.py"), str(record),
                                 "--vault", str(vault), "--phase", phase, "--require-current"],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    validate("plan")
    (vault / output).write_text("---\nstatus: draft\n---\nAttributed to the fixture source.\n", encoding="utf-8")
    ledger_command("update", str(ledger_path), "--section", section["id"], "--status", "ingested",
                   "--output", output, "--expected-revision", str(ledger["revision"]))
    data["execution_status"] = "completed"
    validate("complete")

    spec = importlib.util.spec_from_file_location("draft_query_policy", ROOT / "hermes-obsidian-controlled-query/scripts/locate_source_sections.py")
    query = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(query)
    sys.path.insert(0, str(ROOT / "qmd-like-rag/src"))
    from qmd_like_rag.governance import eligible_corpus_paths
    assert not query.GovernanceQueryPolicy(vault).allows(output)
    assert output not in eligible_corpus_paths(vault)
    # Approval alone still does not activate the source.
    run_manager(vault, "organization-status", "--organization-id", "organization-owner", "--status", "approved",
                "--expected-revision", "1", "--actor", "reviewer")
    assert not query.GovernanceQueryPolicy(vault).allows(output)
    run_manager(vault, "activate", "--version-id", "version-stage-three-1", "--expected-revision", "2", "--actor", "reviewer")
    assert query.GovernanceQueryPolicy(vault).allows(output)
    assert output in eligible_corpus_paths(vault)
    # A shared output also registered under an ineligible source must be excluded.
    excluded = json.loads(ledger_path.read_text(encoding="utf-8"))
    excluded["governance"]["version_id"] = "unresolved-supporting-version"
    (vault / "_system/reports/other.section-ledger.json").write_text(json.dumps(excluded), encoding="utf-8")
    assert not query.GovernanceQueryPolicy(vault).allows(output)
    assert output not in eligible_corpus_paths(vault)
