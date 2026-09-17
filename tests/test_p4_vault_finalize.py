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
sys.path.insert(0, str(ROOT / "qmd-like-rag/src"))

from hermes_source_units import (ContractError, FileKnowledgeBuildService,
                                 FileSourceUnitService,
                                 FileVaultFinalizeService)
import hermes_source_units.vault_finalize as finalize_module
from qmd_like_rag.corpus import load_release_corpus
from test_p3_knowledge_build import publish_sources, run_pass

BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap/scripts/init_obsidian_vault.py"
FINALIZE_CLI = ROOT / "hermes-obsidian-knowledge-finalize/scripts/manage_vault_finalize.py"
LINT = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"
QUERY_RETRIEVE = ROOT / "hermes-obsidian-controlled-query/scripts/retrieve_candidates.py"


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


def test_p5_release_corpus_reads_every_eligible_canonical_unit_once(vault: Path):
    refs = publish_sources(vault, ["# Pump X\nTransfers coolant and requires filtered inlet fluid.\n"])
    completed_page(vault, refs)
    finalize = FileVaultFinalizeService(vault)
    apply(finalize, finalize.plan(request("release-p5", ["run-initial"]))["plan"])
    corpus = load_release_corpus(vault)
    units = FileSourceUnitService(vault).list(refs[0]["unit_ref"]["resource_id"],
                                              refs[0]["unit_ref"]["unit_set_id"])
    projected = [item for item in corpus.projections if item["projection_kind"] == "source_unit"]
    assert len(projected) == len(units)
    assert {item["unit_ref"]["unit_id"] for item in projected} == {
        item["ref"]["unit_id"] for item in units}
    assert all(item["core_text"] for item in projected)
    assert not any(item["projection_kind"] == "knowledge_page" for item in corpus.projections)


def test_p5_query_revalidates_unitref_and_reads_exact_subspan(vault: Path, tmp_path: Path):
    refs = publish_sources(vault, ["# Pump X\nTransfers coolant and requires filtered inlet fluid.\n"])
    completed_page(vault, refs)
    finalize = FileVaultFinalizeService(vault)
    apply(finalize, finalize.plan(request("release-query", ["run-initial"]))["plan"])
    corpus = load_release_corpus(vault)
    projection = next(item for item in corpus.projections if item["projection_kind"] == "source_unit")
    selected = "Transfers coolant"
    relative_start = projection["core_text"].index(selected)
    absolute_start = projection["locator_span"]["start"] + relative_start
    candidate = {"vault_path": projection["vault_path"], "line_start": projection["line_start"],
                 "line_end": projection["line_end"], "source_sha256": projection["source_sha256"],
                 "snippet": "untrusted snippet", "score": 0.9, "projection_kind": "source_unit",
                 "projection_fingerprint": "sha256:projection", "release_id": corpus.release_id,
                 "release_hash": corpus.release_hash, "unit_ref": projection["unit_ref"],
                 "page_id": None, "page_revision_id": None, "source_unit_refs": [],
                 "subspan": {"start": absolute_start, "end": absolute_start + len(selected)},
                 "subspan_reason": "oversized-protected-structure",
                 "projection_content_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest()}
    payload = {"protocol_version": "hermes-coarse-recall/v1", "provider": "qmd-like-rag",
               "provider_version": "test", "status": "ok", "authority": "candidate-navigation-only",
               "index_fingerprint": "sha256:index", "release_id": corpus.release_id,
               "release_hash": corpus.release_hash, "index_generation": "generation-test",
               "capabilities": {"source_units": True, "release_driven": True,
                                "projection_kinds": ["source_unit", "knowledge_page"]},
               "warnings": [], "candidates": [candidate]}
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import argparse,json\n"
        "p=argparse.ArgumentParser();p.add_argument('command');p.add_argument('--vault-root');"
        "p.add_argument('--query');p.add_argument('--top-k');p.parse_args()\n"
        f"print(json.dumps({payload!r}))\n", encoding="utf-8")
    config = tmp_path / "provider.json"
    config.write_text(json.dumps({"provider": "qmd-like-rag", "transport": "command",
                                  "command": [sys.executable, str(provider)]}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(QUERY_RETRIEVE), str(vault), "coolant",
                             "--provider-config", str(config)], capture_output=True, text=True,
                            encoding="utf-8", check=True)
    value = json.loads(result.stdout)
    assert value["status"] == "ok"
    assert value["candidates"][0]["exact_source_unit"] is True
    assert value["candidates"][0]["core_text"] == selected
    assert value["candidates"][0]["core_text"] != "untrusted snippet"


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
