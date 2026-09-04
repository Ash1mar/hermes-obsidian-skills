from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap" / "scripts" / "init_obsidian_vault.py"


def test_bootstrap_keeps_runtime_skills_outside_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(vault),
            "--profile",
            "general",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (vault / "_system" / "skills").exists()
    assert (vault / "_system" / "templates" / "ingest-log-template.md").is_file()
    report = next((vault / "_system" / "reports").glob("vault-setup-*.md"))
    assert "copied skill note" not in report.read_text(encoding="utf-8")
    agents = (vault / "AGENTS.md").read_text(encoding="utf-8")
    ingest_rules = (vault / "_system" / "prompts" / "hermes-ingest-rules.md").read_text(encoding="utf-8")
    assert "外部 skill 仓库" not in ingest_rules
    assert "external skill repository" not in ingest_rules
    assert ".hermes/skills" not in agents
    assert ".hermes/skills" not in ingest_rules


def test_bootstrap_cli_cannot_persist_a_skill_repository_path() -> None:
    completed = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--skill-repo" not in completed.stdout


def test_bootstrap_uses_deployment_vault_without_runtime_path_flag(tmp_path: Path) -> None:
    vault = tmp_path / "configured-vault"
    config = tmp_path / "deployment.json"
    config.write_text(json.dumps({"vault_path": str(vault)}), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--deployment-config",
            str(config),
            "--profile",
            "general",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (vault / "AGENTS.md").is_file()


def test_engineering_profile_creates_database_ready_json_governance(tmp_path: Path) -> None:
    vault = tmp_path / "engineering-vault"
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(vault),
            "--profile",
            "engineering",
            "--vault-id",
            "vault-north-plant",
            "--vault-name",
            "North Plant",
            "--security-domain",
            "internal-a",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((vault / "_system" / "vault.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (vault / "_system" / "metadata" / "document-governance.schema.json").read_text(encoding="utf-8")
    )
    organizations = json.loads(
        (vault / "_system" / "metadata" / "source-organizations.json").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (vault / "_system" / "metadata" / "document-registry.json").read_text(encoding="utf-8")
    )

    assert manifest["vault"] == {
        "id": "vault-north-plant",
        "name": "North Plant",
        "profile": "engineering",
        "security_domain": "internal-a",
    }
    assert manifest["governance"]["readiness"] == "draft"
    assert manifest["governance"]["repository"] == {
        "contract": "hermes-governance/v1",
        "backend": "json",
        "registry_path": "_system/metadata/document-registry.json",
    }
    assert set(schema["x-hermes-database-mapping"]["planned_backends"]) == {"sqlite", "postgresql"}
    assert organizations["organizations"] == []
    assert organizations["events"] == []
    assert registry["records"] == []
    assert registry["events"] == []
    assert registry["registry_revision"] == 0


def test_engineering_profile_rejects_invalid_stable_id(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(tmp_path / "vault"),
            "--profile",
            "engineering",
            "--vault-id",
            "Invalid ID",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "--vault-id must match" in completed.stderr


def test_engineering_profile_never_overwrites_existing_governance(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    governance = vault / "_system" / "vault.json"
    governance.parent.mkdir(parents=True)
    governance.write_text('{"keep": true}\n', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(vault),
            "--profile",
            "engineering",
            "--force-empty",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "bootstrap does not overwrite or upgrade them" in completed.stderr
    assert governance.read_text(encoding="utf-8") == '{"keep": true}\n'
