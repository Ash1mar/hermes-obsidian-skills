"""P4 incremental Vault Finalize acceptance on temporary Vaults."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))

from hermes_source_units import (ContractError, FileKnowledgeBuildService,
                                 FileSourceUnitService,
                                 FileVaultFinalizeService)
import hermes_source_units.vault_finalize as finalize_module
from test_p3_knowledge_build import publish_sources, run_pass

BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap/scripts/init_obsidian_vault.py"
FINALIZE_CLI = ROOT / "hermes-obsidian-knowledge-finalize/scripts/manage_vault_finalize.py"
LINT = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(BOOTSTRAP), "--vault-path", str(target)],
        capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def completed_page(vault: Path, refs: list[dict], *, run_id: str = "run-initial",
                   path: str = "30_Cards/pump-x.md", content: str = "# Pump X\n\nEvidence.\n",
                   action: str = "create", parent_hash: str | None = None,
                   identity_key: str = "project-a:pump-x",
                   canonical_name: str = "Pump X", aliases: list[str] | None = None) -> dict:
    service = FileKnowledgeBuildService(vault)
    tasks, passes = [], []
    for index, ref in enumerate(refs, 1):
        task, citation = run_pass(service, f"task-{run_id}-{index}", ref)
        tasks.append({"task_id": task["task_id"], "revision": task["revision"]})
        passes.append({"pass_id": citation["pass_id"], "candidate_id": "pump"})
    expected = service.list_identities()["revision"]
    reduced = service.reduce({
        "run_id": run_id, "actor": "agent", "tasks": tasks,
        "expected_registry_revision": expected, "document_registry_revision": 1,
        "decisions": [{"candidate_refs": passes,
                       "identity": {"kind": "entity", "identity_key": identity_key,
                                    "canonical_name": canonical_name,
                                    "aliases": ["PX"] if aliases is None else aliases},
                       "action": action, "path": path, "content": content}],
        "reason": "P4 fixture build",
    })
    page = reduced["run"]["page_revisions"][0]
    service.finalize({
        "run_id": run_id, "actor": "agent", "expected_revision": 1,
        "reviews": [{"page_id": page["page_id"], "authored_sha256": page["authored_sha256"],
                     "parent_authored_sha256": parent_hash, "actor": "reviewer",
                     "note": "Reviewed exact source support"}],
    })
    return page


def request(release_id: str, builds: list[str], *, state: int = 0,
            changes: list[dict] | None = None, reason: str = "bounded release") -> dict:
    return {"release_id": release_id, "actor": "release-agent",
            "expected_state_revision": state, "build_run_ids": builds,
            "source_changes": changes or [], "reason": reason}


def apply(service: FileVaultFinalizeService, plan: dict) -> dict:
    return service.apply({"release_id": plan["release_id"], "actor": plan["actor"],
                          "plan_id": plan["plan_id"], "expected_revision": plan["revision"]})


def test_release_is_auditable_idempotent_and_creates_safe_move_redirect(vault: Path):
    refs = publish_sources(vault, ["# Pump X\nTransfers coolant.\n"])
    first = completed_page(vault, refs)
    service = FileVaultFinalizeService(vault)
    planned = service.plan(request("release-1", ["run-initial"]))["plan"]
    assert planned["subjects"][0]["disposition"] == "current"
    source_item = next(item for item in planned["index_eligibility"] if item["kind"] == "source_unit_set")
    page_item = next(item for item in planned["index_eligibility"] if item["kind"] == "knowledge_page")
    assert source_item["eligible"] is True
    assert page_item["eligible"] is False
    assert page_item["reasons"] == ["business-unassessed", "visibility-draft"]
    committed = apply(service, planned)
    assert committed["committed"] is True
    assert apply(service, planned)["committed"] is False
    assert service.validate("release-1")["subject_count"] == 1

    moved = completed_page(vault, refs, run_id="run-move", action="update",
                           path="30_Cards/equipment/pump-x.md",
                           content="# Pump X\n\nMoved and updated.\n",
                           parent_hash=first["authored_sha256"])
    next_plan = service.plan(request("release-2", ["run-move"], state=1))["plan"]
    assert next_plan["redirects"] == [{
        "from_path": "30_Cards/pump-x.md", "to_path": "30_Cards/equipment/pump-x.md",
        "page_id": moved["page_id"], "source_sha256": first["authored_sha256"]}]
    apply(service, next_plan)
    redirect = (vault / "30_Cards/pump-x.md").read_text(encoding="utf-8")
    assert "hermes_redirect: true" in redirect and "30_Cards/equipment/pump-x" in redirect
    assert service.validate("release-1")["ok"]
    assert service.validate("release-2")["ok"]
    assert service.status()["applied_build_runs"] == ["run-initial", "run-move"]
    lint = subprocess.run([sys.executable, "-I", "-S", str(LINT), "--vault", str(vault),
                           "--profile", "post-ingest", "--json"],
                          capture_output=True, text=True)
    report = json.loads(lint.stdout)
    assert report["metrics"]["knowledge_releases"] == 2
    assert not any(item["code"] == "source_units.knowledge_release_invalid"
                   for item in report["issues"])


def replace_source(vault: Path, old_ref: dict) -> dict:
    path = vault / "10_Raw/source-1.md"
    path.write_text("# Pump X\nReplacement source text.\n", encoding="utf-8", newline="\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    registry_path = vault / "_system/metadata/document-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["records"][0]["content_sha256"] = digest
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    source = FileSourceUnitService(vault)
    prepared = source.prepare_markdown("10_Raw/source-1.md", "doc-1", "version-1", "resource-1")
    built = source.build({"artifact_manifest": prepared["artifact_manifest"], "config": source.config,
                          "actor": "builder", "expected_revision": 1})
    assert built["manifest"]["unit_set_id"] != old_ref["unit_ref"]["unit_set_id"]
    return built["manifest"]


def test_replacement_marks_only_invalidated_contribution_and_keeps_page(vault: Path):
    refs = publish_sources(vault, ["# Pump\nPrimary evidence.\n", "# Limit\nIndependent evidence.\n"])
    completed_page(vault, refs)
    service = FileVaultFinalizeService(vault)
    apply(service, service.plan(request("release-1", ["run-initial"]))["plan"])
    replacement = replace_source(vault, refs[0])
    change = {"resource_id": "resource-1",
              "previous_unit_set_id": refs[0]["unit_ref"]["unit_set_id"],
              "current_unit_set_id": replacement["unit_set_id"], "change": "replaced",
              "reason": "source content was revised"}
    plan = service.plan(request("release-2", [], state=1, changes=[change]))["plan"]
    subject = plan["subjects"][0]
    assert subject["disposition"] == "review_required"
    assert subject["stale_support_refs"] == [refs[0]]
    assert subject["active_support_refs"] == [refs[1]]
    assert (vault / subject["current_path"]).is_file()
    apply(service, plan)
    assert service.validate("release-2")["blocked_count"] == 0


def test_withdrawal_without_remaining_support_blocks_page_but_commits_release(vault: Path):
    refs = publish_sources(vault, ["# Pump\nOnly evidence.\n"])
    completed_page(vault, refs)
    service = FileVaultFinalizeService(vault)
    apply(service, service.plan(request("release-1", ["run-initial"]))["plan"])
    change = {"resource_id": "resource-1",
              "previous_unit_set_id": refs[0]["unit_ref"]["unit_set_id"],
              "current_unit_set_id": None, "change": "withdrawn",
              "reason": "source authority withdrew the material"}
    plan = service.plan(request("release-2", [], state=1, changes=[change]))["plan"]
    assert plan["subjects"][0]["disposition"] == "blocked"
    assert "no-current-source-support" in plan["subjects"][0]["reasons"]
    source_item = next(item for item in plan["index_eligibility"] if item["kind"] == "source_unit_set")
    assert source_item["eligible"] is False and "source-withdrawn" in source_item["reasons"]
    apply(service, plan)
    assert service.validate("release-2")["blocked_count"] == 1


def test_dead_link_blocks_apply_without_advancing_release_state(vault: Path):
    refs = publish_sources(vault, ["# Pump\nEvidence.\n"])
    completed_page(vault, refs, content="# Pump X\n\n[[Missing target]]\n")
    service = FileVaultFinalizeService(vault)
    plan = service.plan(request("release-dead-link", ["run-initial"]))["plan"]
    assert plan["blockers"] == [f"dead-link:{plan['subjects'][0]['page_id']}:Missing target"]
    with pytest.raises(ContractError, match="unresolved blockers"):
        apply(service, plan)
    assert service.status()["revision"] == 0
    assert not (vault / "_system/knowledge-releases/release-dead-link/manifest.json").exists()


def test_ambiguous_alias_is_a_navigation_blocker(vault: Path):
    refs = publish_sources(vault, ["# Objects\nEvidence.\n"])
    completed_page(vault, refs, run_id="run-a", path="30_Cards/a.md",
                   identity_key="project:a", canonical_name="Object A", aliases=["Shared"])
    completed_page(vault, refs, run_id="run-b", path="30_Cards/b.md",
                   identity_key="project:b", canonical_name="Object B", aliases=["Shared"])
    service = FileVaultFinalizeService(vault)
    plan = service.plan(request("release-alias", ["run-a", "run-b"]))["plan"]
    assert any(item.startswith("ambiguous-alias:shared:") for item in plan["blockers"])
    with pytest.raises(ContractError, match="unresolved blockers"):
        apply(service, plan)


def test_apply_recovers_when_manifest_last_write_was_interrupted(vault: Path, monkeypatch):
    refs = publish_sources(vault, ["# Pump\nEvidence.\n"])
    completed_page(vault, refs)
    service = FileVaultFinalizeService(vault)
    plan = service.plan(request("release-recovery", ["run-initial"]))["plan"]
    real_write = finalize_module._write_atomic
    manifest = (vault / "_system/knowledge-releases/release-recovery/manifest.json").resolve()

    def interrupted(path: Path, data: bytes) -> None:
        if Path(path).resolve() == manifest:
            raise OSError("simulated final manifest interruption")
        real_write(path, data)

    monkeypatch.setattr(finalize_module, "_write_atomic", interrupted)
    with pytest.raises(OSError, match="simulated"):
        apply(service, plan)
    assert service.status()["current_release_id"] == "release-recovery"
    assert not manifest.exists()
    monkeypatch.setattr(finalize_module, "_write_atomic", real_write)
    recovered = apply(service, plan)
    assert recovered["committed"] is True
    assert service.validate("release-recovery")["ok"]


def test_p4_cli_uses_only_its_embedded_runtime(vault: Path):
    result = subprocess.run([sys.executable, "-I", "-S", str(FINALIZE_CLI), str(vault), "status"],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["contract"] == "hermes-knowledge-release-state/v1"
