"""P3 Pass/Reduce and Build Finalize acceptance on temporary Vaults."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))

from hermes_source_units import ContractError, FileKnowledgeBuildService, FileSourceUnitService

BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap/scripts/init_obsidian_vault.py"
LINT = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"
KNOWLEDGE_CLI = ROOT / "hermes-obsidian-controlled-ingest/scripts/manage_knowledge_build.py"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(BOOTSTRAP), "--vault-path", str(target)],
        capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def publish_sources(vault: Path, texts: list[str]) -> list[dict]:
    records = []
    source_paths = []
    for index, text in enumerate(texts, 1):
        path = vault / f"10_Raw/source-{index}.md"
        path.write_text(text, encoding="utf-8", newline="\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({
            "document_id": f"doc-{index}", "version_id": f"version-{index}",
            "resource_id": f"resource-{index}", "content_sha256": digest,
            "processing_status": "completed", "governance_status": "active",
            "source_occurrences": [{"source_organization_id": "organization-demo"}],
        })
        source_paths.append(path)
    registry_path = vault / "_system/metadata/document-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry.update({"registry_revision": 1, "records": records})
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    organizations_path = vault / "_system/metadata/source-organizations.json"
    organizations = json.loads(organizations_path.read_text(encoding="utf-8"))
    organizations.update({"registry_revision": 1, "organizations": [
        {"id": "organization-demo", "status": "approved"}]})
    organizations_path.write_text(json.dumps(organizations), encoding="utf-8")

    source = FileSourceUnitService(vault)
    refs = []
    for index, path in enumerate(source_paths, 1):
        prepared = source.prepare_markdown(
            path.relative_to(vault).as_posix(), f"doc-{index}", f"version-{index}", f"resource-{index}")
        built = source.build({"artifact_manifest": prepared["artifact_manifest"],
                              "config": source.config, "actor": "builder", "expected_revision": 0})
        text_unit = next(item for item in built["units"] if item["locator"]["kind"] == "text")
        refs.append({"unit_ref": text_unit["ref"], "span": None})
    return refs


def run_pass(service: FileKnowledgeBuildService, task_id: str, ref: dict,
             candidate: bool = True, qa: str = "usable") -> tuple[dict, dict]:
    service.plan_task({"task_id": task_id, "actor": "agent", "target_refs": [ref],
                       "expected_revision": 0, "registry_revision": 1})
    claimed = service.claim_task(task_id, "agent", 1)["task"]
    package = service.reading_material(task_id, "agent", 1)["package"]
    inspection = {"source_ref": ref, "finding": f"inspected {task_id}", "qa": qa,
                  "qa_note": "symbol requires review" if qa == "needs-qa" else ""}
    pass0_request = {"task_id": task_id, "actor": "agent", "expected_revision": claimed["revision"],
                     "reading_package_id": package["package_id"], "registry_revision": 1,
                     "pass_kind": "candidate", "sequence": 0, "inspections": [inspection],
                     "candidates": ([{"candidate_id": "pump", "name": "Pump X", "kind": "entity",
                                      "identity_rationale": "same project and model",
                                      "finding": "candidate object", "applicability": "Project A",
                                      "conditions": [], "exceptions": [], "support_refs": [ref]}]
                                    if candidate else []),
                     "empty_reason": "" if candidate else "No reusable knowledge candidate"}
    pass0 = service.record_pass(pass0_request)
    assert service.record_pass(pass0_request)["created"] is False
    if not candidate:
        return pass0["task"], pass0["pass"]
    citation = service.record_pass({
        **pass0_request, "expected_revision": pass0["task"]["revision"],
        "pass_kind": "citation", "sequence": 1,
        "candidates": [{"candidate_id": "pump", "name": "Pump X", "kind": "entity",
                        "identity_rationale": "same project and model",
                        "finding": f"evidence from {task_id}", "applicability": "Project A",
                        "conditions": ["filtered inlet fluid"] if qa == "needs-qa" else [],
                        "exceptions": [], "support_refs": [ref]}],
    })
    return citation["task"], citation["pass"]


def test_cross_source_pass_reduce_finalize_and_stable_identity(vault: Path):
    refs = publish_sources(vault, [
        "# Pump X\nPump X transfers coolant in Project A.\n",
        "# Limit\nPump X requires filtered inlet fluid.\n",
    ])
    service = FileKnowledgeBuildService(vault)
    first_task, first_pass = run_pass(service, "task-a", refs[0])
    second_task, second_pass = run_pass(service, "task-b", refs[1], qa="needs-qa")
    reduced = service.reduce({
        "run_id": "run-cross-source", "actor": "agent",
        "tasks": [{"task_id": "task-a", "revision": first_task["revision"]},
                  {"task_id": "task-b", "revision": second_task["revision"]}],
        "expected_registry_revision": 0, "document_registry_revision": 1,
        "decisions": [{
            "candidate_refs": [{"pass_id": first_pass["pass_id"], "candidate_id": "pump"},
                               {"pass_id": second_pass["pass_id"], "candidate_id": "pump"}],
            "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                         "canonical_name": "Pump X", "aliases": ["PX"]},
            "action": "create", "path": "30_Cards/pump-x.md",
            "content": "# Pump X\n\nTransfers coolant and requires filtered inlet fluid.\n",
        }],
        "reason": "Reduced two source contributions",
    })
    page = reduced["run"]["page_revisions"][0]
    assert page["qa_status"] == "qa_required"
    assert page["business_status"] == "unassessed" and page["visibility"] == "draft"
    finalized = service.finalize({
        "run_id": "run-cross-source", "actor": "agent", "expected_revision": 1,
        "reviews": [{"page_id": page["page_id"], "authored_sha256": page["authored_sha256"],
                     "parent_authored_sha256": None, "actor": "reviewer",
                     "note": "Compared both source contributions and retained the QA boundary"}],
    })
    assert finalized["run"]["state"] == "completed"
    assert len(finalized["run"]["page_revisions"][0]["support_refs"]) == 2
    assert service.validate_run("run-cross-source")["ok"]
    identity = service.list_identities()["subjects"][0]
    assert identity["page_id"] == page["page_id"]
    assert (vault / identity["current_path"]).read_text(encoding="utf-8").startswith("# Pump X")
    assert service._task("task-a")["status"] == service._task("task-b")["status"] == "completed"
    assert service.finalize({"run_id": "run-cross-source", "actor": "agent",
                             "expected_revision": 1, "reviews": []})["committed"] is False

    update_task, update_pass = run_pass(service, "task-update", refs[0])
    update = service.reduce({
        "run_id": "run-move", "actor": "agent",
        "tasks": [{"task_id": "task-update", "revision": update_task["revision"]}],
        "expected_registry_revision": 1, "document_registry_revision": 1,
        "decisions": [{
            "candidate_refs": [{"pass_id": update_pass["pass_id"], "candidate_id": "pump"}],
            "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                         "canonical_name": "Pump X", "aliases": ["PX", "Pump-X"]},
            "action": "update", "path": "30_Cards/equipment/pump-x.md",
            "content": "# Pump X\n\nUpdated reader-facing description.\n",
        }], "reason": "Updated content and moved the path projection",
    })
    updated_page = update["run"]["page_revisions"][0]
    assert updated_page["page_id"] == page["page_id"]
    service.finalize({
        "run_id": "run-move", "actor": "agent", "expected_revision": 1,
        "reviews": [{"page_id": page["page_id"], "authored_sha256": updated_page["authored_sha256"],
                     "parent_authored_sha256": page["authored_sha256"], "actor": "reviewer",
                     "note": "Reviewed the update and path projection"}],
    })
    moved = service.list_identities()["subjects"][0]
    assert moved["subject_id"] == identity["subject_id"]
    assert moved["current_path"] == "30_Cards/equipment/pump-x.md"
    assert service.validate_run("run-cross-source")["ok"]
    assert service.validate_run("run-move")["ok"]
    lint = subprocess.run([sys.executable, "-I", "-S", str(LINT), "--vault", str(vault),
                           "--profile", "post-ingest", "--json"],
                          capture_output=True, text=True)
    lint_result = json.loads(lint.stdout)
    assert lint_result["metrics"]["unit_knowledge_build_runs"] == 2
    assert not any(item["code"] == "source_units.knowledge_build_invalid"
                   for item in lint_result["issues"])


def test_empty_pass_can_finalize_without_identity_or_page(vault: Path):
    ref = publish_sources(vault, ["# Administrative note\nNo reusable object is defined.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    task, _ = run_pass(service, "task-empty", ref, candidate=False)
    reduced = service.reduce({
        "run_id": "run-empty", "actor": "agent",
        "tasks": [{"task_id": "task-empty", "revision": task["revision"]}],
        "expected_registry_revision": 0, "document_registry_revision": 1,
        "decisions": [], "reason": "Inspected the complete target; no reusable candidate found",
    })
    finalized = service.finalize({"run_id": "run-empty", "actor": "agent",
                                  "expected_revision": reduced["run"]["revision"], "reviews": []})
    assert finalized["run"]["page_revisions"] == []
    assert service.list_identities()["revision"] == 0
    assert service._task("task-empty")["outputs"] == []


def test_overlap_is_reported_and_interruption_retry_increments_attempt(vault: Path):
    ref = publish_sources(vault, ["# Source\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    service.plan_task({"task_id": "task-one", "actor": "agent", "target_refs": [ref],
                       "expected_revision": 0, "registry_revision": 1})
    overlap = service.plan_task({"task_id": "task-two", "actor": "agent", "target_refs": [ref],
                                 "expected_revision": 0, "registry_revision": 1})
    assert overlap["overlapping_tasks"] == ["task-one"]
    claimed = service.claim_task("task-one", "agent", 1)["task"]
    assert service.claim_task("task-one", "agent", 1)["claimed"] is False
    blocked = service.set_task_state("task-one", "agent", claimed["revision"], "blocked",
                                     "external review required")["task"]
    resumed = service.claim_task("task-one", "agent", blocked["revision"])["task"]
    assert resumed["attempt"] == 2 and resumed["status"] == "running"


def test_p3_cli_runs_from_embedded_skill_runtime(vault: Path):
    result = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                             "--vault", str(vault), "identities"],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["registry"]["subjects"] == []


def test_reduce_rejects_pass0_as_final_citation(vault: Path):
    ref = publish_sources(vault, ["# Pump\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    task, _ = run_pass(service, "task-only-pass0", ref, candidate=True)
    pass0_path = next((vault / "_system/knowledge-builds/task-task-only-pass0/passes").glob("0000-*.json"))
    pass0 = json.loads(pass0_path.read_text(encoding="utf-8"))
    with pytest.raises(ContractError, match="Pass 1..N"):
        service.reduce({
            "run_id": "run-invalid", "actor": "agent",
            "tasks": [{"task_id": "task-only-pass0", "revision": task["revision"]}],
            "expected_registry_revision": 0, "document_registry_revision": 1,
            "decisions": [{"candidate_refs": [{"pass_id": pass0["pass_id"], "candidate_id": "pump"}],
                           "identity": {"kind": "entity", "identity_key": "pump",
                                        "canonical_name": "Pump", "aliases": []},
                           "action": "create", "path": "30_Cards/pump.md", "content": "# Pump\n"}],
            "reason": "invalid",
        })


def test_context_inspection_does_not_complete_task_target(vault: Path):
    publish_sources(vault, ["# Long section\n" + ("alpha sentence. " * 120)])
    units = [item for item in FileSourceUnitService(vault).list("resource-1")
             if item["locator"]["kind"] == "text"]
    assert len(units) >= 2
    target_ref = {"unit_ref": units[1]["ref"], "span": None}
    service = FileKnowledgeBuildService(vault)
    service.plan_task({"task_id": "task-context", "actor": "agent", "target_refs": [target_ref],
                       "expected_revision": 0, "registry_revision": 1})
    task = service.claim_task("task-context", "agent", 1)["task"]
    package = service.reading_material("task-context", "agent", 1)["package"]
    context = next(item for item in package["materials"] if item["role"] == "context")
    recorded = service.record_pass({
        "task_id": "task-context", "actor": "agent", "expected_revision": task["revision"],
        "reading_package_id": package["package_id"], "registry_revision": 1,
        "pass_kind": "candidate", "sequence": 0,
        "inspections": [{"source_ref": context["source_ref"], "finding": "context only",
                         "qa": "usable", "qa_note": ""}],
        "candidates": [], "empty_reason": "Context had no candidate",
    })
    with pytest.raises(ContractError, match="INCOMPLETE_COVERAGE"):
        service.reduce({"run_id": "run-context", "actor": "agent",
                        "tasks": [{"task_id": "task-context",
                                   "revision": recorded["task"]["revision"]}],
                        "expected_registry_revision": 0, "document_registry_revision": 1,
                        "decisions": [], "reason": "invalid context-only completion"})


def test_finalize_preflight_rejects_drift_without_committing_control_state(vault: Path):
    ref = publish_sources(vault, ["# Pump\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    task, citation = run_pass(service, "task-drift", ref)
    reduced = service.reduce({
        "run_id": "run-drift", "actor": "agent",
        "tasks": [{"task_id": "task-drift", "revision": task["revision"]}],
        "expected_registry_revision": 0, "document_registry_revision": 1,
        "decisions": [{"candidate_refs": [{"pass_id": citation["pass_id"], "candidate_id": "pump"}],
                       "identity": {"kind": "entity", "identity_key": "drift:pump",
                                    "canonical_name": "Pump", "aliases": []},
                       "action": "create", "path": "30_Cards/drift-pump.md", "content": "# Pump\n"}],
        "reason": "drift test",
    })
    page = reduced["run"]["page_revisions"][0]
    target = vault / page["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unrelated existing page\n", encoding="utf-8")
    with pytest.raises(ContractError, match="SOURCE_CHANGED"):
        service.finalize({"run_id": "run-drift", "actor": "agent", "expected_revision": 1,
                          "reviews": [{"page_id": page["page_id"],
                                       "authored_sha256": page["authored_sha256"],
                                       "parent_authored_sha256": None, "actor": "reviewer",
                                       "note": "reviewed"}]})
    assert service.list_identities()["revision"] == 0
    assert service._task("task-drift")["status"] == "running"
    assert service.validate_run("run-drift")["state"] == "draft"
