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

from hermes_source_units import (ContractError, FileIngestWorkflowService,
                                 FileKnowledgeBuildService, FileSourceUnitService,
                                 mutation_digest)

BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap/scripts/init_obsidian_vault.py"
LINT = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"
KNOWLEDGE_CLI = ROOT / "hermes-obsidian-controlled-ingest/scripts/manage_knowledge_build.py"
WORKFLOW_CLI = ROOT / "hermes-obsidian-controlled-ingest/scripts/manage_ingest_workflow.py"


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


def publish_oversize_text_asset(vault: Path) -> dict:
    bundle = vault / "10_Raw/converted/oversize_bundle"
    (bundle / "tables").mkdir(parents=True)
    document = "# Oversize asset\nSee table-oversize.\n"
    asset = ("cell-value|" * 1400).encode("utf-8")
    (bundle / "document.md").write_text(document, encoding="utf-8", newline="\n")
    (bundle / "tables/oversize.md").write_bytes(asset)
    outline = {"schema_version": "2.0", "sections": [{
        "id": "asset", "title": "Oversize asset", "level": 1, "parent": None,
        "path": ["Oversize asset"], "start_line": 1, "end_line": 2,
        "pages": [1], "assets": ["table-oversize"], "quality": "pass",
    }]}
    (bundle / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    source_sha = hashlib.sha256(b"oversize-source").hexdigest()
    manifest = {
        "schema_version": "2.0", "source": {"sha256": source_sha},
        "document": {"path": "document.md"}, "outline": {"path": "outline.json"},
        "images": [], "tables": [{"id": "table-oversize", "path": "tables/oversize.md",
                                      "sha256": hashlib.sha256(asset).hexdigest(),
                                      "media_type": "text/markdown", "pages": [1]}],
        "governance": {"vault_id": json.loads((vault / "_system/vault.json").read_text())["vault"]["id"],
                       "document_id": "doc-asset", "version_id": "version-asset",
                       "resource_id": "resource-asset"},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    registry_path = vault / "_system/metadata/document-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry.update({"registry_revision": 1, "records": [{
        "document_id": "doc-asset", "version_id": "version-asset",
        "resource_id": "resource-asset", "content_sha256": source_sha,
        "processing_status": "completed", "governance_status": "active",
        "source_occurrences": [{"source_organization_id": "organization-demo"}],
    }]})
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    organizations_path = vault / "_system/metadata/source-organizations.json"
    organizations = json.loads(organizations_path.read_text(encoding="utf-8"))
    organizations.update({"registry_revision": 1, "organizations": [
        {"id": "organization-demo", "status": "approved"}]})
    organizations_path.write_text(json.dumps(organizations), encoding="utf-8")
    source = FileSourceUnitService(vault)
    prepared = source.prepare_bundle("10_Raw/converted/oversize_bundle")
    built = source.build({"artifact_manifest": prepared["artifact_manifest"],
                          "config": source.config, "actor": "builder", "expected_revision": 0})
    unit = next(item for item in built["units"] if item["locator"]["kind"] == "asset")
    return {"unit_ref": unit["ref"], "span": None}


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
    transitioned = service.set_task_state_batch({
        "operation_id": "supersede-one", "actor": "agent",
        "transitions": [{"task_id": "task-two", "expected_revision": 1,
                         "status": "blocked", "reason": "superseded by regenerated batch",
                         "deferred": []}],
    })
    assert transitioned["results"] == [{"task_id": "task-two", "status": "blocked", "revision": 2}]
    claimed = service.claim_task("task-one", "agent", 1)["task"]
    assert service.claim_task("task-one", "agent", 1)["claimed"] is False
    blocked = service.set_task_state("task-one", "agent", claimed["revision"], "blocked",
                                     "external review required")["task"]
    resumed = service.claim_task("task-one", "agent", blocked["revision"])["task"]
    assert resumed["attempt"] == 2 and resumed["status"] == "running"


def test_batch_session_cache_invalidates_when_artifact_changes(vault: Path):
    ref = publish_sources(vault, ["# Cached source\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    service.source.enable_session_cache()
    access = {"actor": "agent", "purpose": "construction", "registry_revision": 1}
    assert service.source.get({"source_ref": ref, "access": access})["core_text"]
    document = (vault / "_system/sources/artifacts" / ref["unit_ref"]["resource_id"]
                / ref["unit_ref"]["artifact_revision"] / "document.md")
    document.write_text(document.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
    with pytest.raises(ContractError, match="SOURCE_CHANGED"):
        service.source.get({"source_ref": ref, "access": access})


def test_batch_workflow_preserves_legacy_artifacts_and_reaches_checkpoint(vault: Path):
    refs = publish_sources(vault, [
        "# Pump X\nPump X transfers coolant.\n",
        "# Constraint\nPump X requires filtered fluid.\n",
    ])
    service = FileKnowledgeBuildService(vault)
    planned = service.plan_batch({
        "batch_id": "batch-one", "actor": "agent", "registry_revision": 1,
        "tasks": [
            {"task_id": "batch-one-resource-1-part001", "target_refs": [refs[0]],
             "expected_revision": 0},
            {"task_id": "batch-one-resource-2-part001", "target_refs": [refs[1]],
             "expected_revision": 0},
        ],
    })
    assert planned["created"] and planned["created_tasks"] == 2
    assert service.plan_batch({
        "batch_id": "batch-one", "actor": "agent", "registry_revision": 1,
        "tasks": [
            {"task_id": "batch-one-resource-1-part001", "target_refs": [refs[0]]},
            {"task_id": "batch-one-resource-2-part001", "target_refs": [refs[1]]},
        ],
    })["created"] is False

    prepared = service.prepare_batch("batch-one", "agent", 1)
    assert prepared["ok"] and len(prepared["results"]) == 2
    packages = {item["task_id"]: item["reading_package_id"] for item in prepared["results"]}
    pass0_requests = []
    for index, ref in enumerate(refs, 1):
        task_id = f"batch-one-resource-{index}-part001"
        task = service._task(task_id)
        pass0_requests.append({
            "task_id": task_id, "actor": "agent", "expected_revision": task["revision"],
            "reading_package_id": packages[task_id], "registry_revision": 1,
            "pass_kind": "candidate", "sequence": 0,
            "inspections": [{"source_ref": ref, "finding": "inspected batch target",
                             "qa": "usable", "qa_note": ""}],
            "candidates": [{"candidate_id": "pump", "name": "Pump X", "kind": "entity",
                            "identity_rationale": "same named project object",
                            "finding": "candidate evidence", "applicability": "Project A",
                            "conditions": [], "exceptions": [], "support_refs": [ref]}],
            "empty_reason": "",
        })
    pass0 = service.record_pass_batch({"batch_id": "batch-one", "passes": pass0_requests})
    assert pass0["ok"] and len(pass0["results"]) == 2

    citation_requests = []
    for request in pass0_requests:
        task = service._task(request["task_id"])
        citation_requests.append({**request, "expected_revision": task["revision"],
                                  "pass_kind": "citation", "sequence": 1})
    citations = service.record_pass_batch({"batch_id": "batch-one", "passes": citation_requests})
    assert citations["ok"]
    pass_ids = {item["task_id"]: item["pass_id"] for item in citations["results"]}
    snapshots = [{"task_id": task_id, "revision": service._task(task_id)["revision"]}
                 for task_id in packages]
    reduced = service.reduce_batch({
        "batch_id": "batch-one",
        "runs": [{
            "run_id": "batch-one-run-001", "actor": "agent", "tasks": snapshots,
            "expected_registry_revision": 0, "document_registry_revision": 1,
            "decisions": [{
                "candidate_refs": [{"pass_id": pass_ids[task_id], "candidate_id": "pump"}
                                   for task_id in packages],
                "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                             "canonical_name": "Pump X", "aliases": []},
                "action": "create", "path": "30_Cards/batch-pump-x.md",
                "content": "# Pump X\n\nTransfers coolant and requires filtered fluid.\n",
            }], "reason": "Batch-level cross-source Reduce",
        }],
    })
    assert reduced["ok"] and reduced["results"][0]["page_count"] == 1
    checkpoint = service.validate_batch("batch-one")
    assert checkpoint["ok"] and checkpoint["batch"]["state"] == "checkpoint_1"

    run = json.loads((vault / "_system/knowledge-builds/batch-one-run-001/manifest.json").read_text())
    page = run["page_revisions"][0]
    finalized = service.finalize_batch({
        "batch_id": "batch-one", "actor": "agent",
        "finalizations": [{
            "run_id": "batch-one-run-001", "actor": "agent", "expected_revision": 1,
            "reviews": [{"page_id": page["page_id"],
                         "authored_sha256": page["authored_sha256"],
                         "parent_authored_sha256": None, "actor": "reviewer",
                         "note": "Approved the batch draft and both source contributions"}],
        }],
    })
    assert finalized["ok"] and finalized["all_runs_completed"]
    status = service.batch_status("batch-one")
    assert status["batch"]["state"] == "completed"
    assert status["task_counts"] == {"completed": 2}
    assert status["runs"] == [{"run_id": "batch-one-run-001", "state": "completed", "revision": 2}]
    with pytest.raises(ContractError, match="completed tasks"):
        service.plan_batch({
            "batch_id": "batch-two", "actor": "agent", "registry_revision": 1,
            "tasks": [{"task_id": "batch-two-task", "target_refs": [refs[0]],
                       "expected_revision": 0}],
        })


def test_batch_adopt_is_read_only_for_existing_tasks(vault: Path):
    ref = publish_sources(vault, ["# Existing plan\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    service.plan_task({"task_id": "existing-task", "actor": "agent", "target_refs": [ref],
                       "expected_revision": 0, "registry_revision": 1})
    task_path = vault / "_system/ledgers/unit-work/existing-task.json"
    before = task_path.read_bytes()
    report = vault / "_system/reports/existing-plan.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"plan":"existing"}\n', encoding="utf-8")
    adopted = service.adopt_batch({
        "batch_id": "adopted-batch", "actor": "agent", "registry_revision": 1,
        "task_ids": ["existing-task"],
        "origin": {"kind": "adopted", "path": "_system/reports/existing-plan.json"},
    })
    assert adopted["created"]
    assert task_path.read_bytes() == before
    assert service.adopt_batch({
        "batch_id": "adopted-batch", "actor": "agent", "registry_revision": 1,
        "task_ids": ["existing-task"],
        "origin": {"kind": "adopted", "path": "_system/reports/existing-plan.json"},
    })["created"] is False
    assert task_path.read_bytes() == before


def test_exact_reading_measurement_matches_prepare_and_counts_serialized_metadata(vault: Path):
    ref = publish_sources(vault, ["# Exact budget\n" + ("Measured evidence. " * 80)])[0]
    service = FileKnowledgeBuildService(vault)
    request = {
        "batch_id": "exact-batch", "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True,
        "tasks": [{"task_id": "exact-task", "target_refs": [ref], "expected_revision": 0}],
    }
    preview = service.measure_batch(request)
    measurement = preview["results"][0]["measurement"]
    assert preview["ok"] and measurement["fits"]
    assert measurement["metadata_codepoints"] > 0
    assert measurement["serialized_codepoints"] == (
        measurement["core_codepoints"] + measurement["ancestor_codepoints"]
        + measurement["context_codepoints"] + measurement["metadata_codepoints"])
    assert measurement["serialized_codepoints"] <= measurement["limit"]
    assert measurement["input_fingerprint"].startswith("sha256:")

    planned = service.plan_batch(request)
    task = service._task("exact-task")
    assert planned["created"] and task["reading_measurement"] == measurement
    assert task["reader_config_hash"] == measurement["reader_config_hash"]
    prepared = service.prepare_batch("exact-batch", "agent", 1)
    assert prepared["ok"]
    assert prepared["results"][0]["measurement"] == measurement
    package_id = prepared["results"][0]["reading_package_id"]
    package = json.loads((vault / f"_system/knowledge-builds/task-exact-task/readings/{package_id}.json").read_text())
    projection = {"window": package["window"], "materials": package["materials"]}
    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    assert len(serialized) == measurement["serialized_codepoints"]


def test_exact_plan_rejects_oversize_before_writing_and_prepare_detects_config_drift(vault: Path):
    ref = publish_sources(vault, ["# Budget guard\nEvidence.\n"])[0]
    service = FileKnowledgeBuildService(vault)
    oversize = {
        "batch_id": "too-small", "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True, "max_codepoints": 100,
        "tasks": [{"task_id": "too-small-task", "target_refs": [ref]}],
    }
    measured = service.measure_batch(oversize)
    assert measured["ok"] is False
    assert measured["results"][0]["measurement"]["blocking_code"] == "READING_WINDOW_OVERSIZE"
    with pytest.raises(ContractError, match="READING_WINDOW_OVERSIZE"):
        service.plan_batch(oversize)
    assert not (vault / "_system/ledgers/unit-work/too-small-task.json").exists()
    assert not (vault / "_system/ledgers/knowledge-build-batches/too-small.json").exists()

    request = {
        "batch_id": "stale-budget", "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True,
        "tasks": [{"task_id": "stale-budget-task", "target_refs": [ref]}],
    }
    service.plan_batch(request)
    prepared = service.prepare_batch("stale-budget", "agent", 1, max_codepoints=13000)
    assert prepared["ok"] is False
    assert prepared["failures"][0]["code"] == "STALE_PLAN"
    assert service._task("stale-budget-task")["status"] == "pending"


def test_whole_asset_oversize_remains_an_explicit_blocker(vault: Path):
    ref = publish_oversize_text_asset(vault)
    service = FileKnowledgeBuildService(vault)
    request = {
        "batch_id": "whole-asset", "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True,
        "tasks": [{"task_id": "whole-asset-task", "target_refs": [ref]}],
    }
    measurement = service.measure_batch(request)["results"][0]["measurement"]
    assert measurement["core_codepoints"] > measurement["limit"]
    assert measurement["blocking_code"] == "WHOLE_ASSET_OVERSIZE"
    with pytest.raises(ContractError, match="WHOLE_ASSET_OVERSIZE"):
        service.plan_batch(request)
    assert not (vault / "_system/ledgers/unit-work/whole-asset-task.json").exists()


def plan_sliced_batch(vault: Path, count: int, batch_id: str = "slice-batch") -> FileKnowledgeBuildService:
    refs = publish_sources(vault, [f"# Slice {index}\nEvidence {index}.\n" for index in range(count)])
    service = FileKnowledgeBuildService(vault)
    service.plan_batch({
        "batch_id": batch_id, "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True,
        "tasks": [{"task_id": f"{batch_id}-task-{index:03d}", "target_refs": [ref]}
                  for index, ref in enumerate(refs, 1)],
    })
    return service


def prepare_layered_reduce_batch(vault: Path, batch_id: str = "layered-reduce") -> tuple[
        FileKnowledgeBuildService, list[dict], dict[str, str], list[dict]]:
    refs = publish_sources(vault, [
        "# Pump X\nPump X transfers coolant.\n",
        "# Pump X constraint\nPump X requires filtered fluid.\n",
    ])
    service = FileKnowledgeBuildService(vault)
    service.plan_batch({
        "batch_id": batch_id, "actor": "agent", "registry_revision": 1,
        "exact_reading_budget": True,
        "tasks": [{"task_id": f"{batch_id}-task-{index}", "target_refs": [ref]}
                  for index, ref in enumerate(refs, 1)],
    })
    prepared = service.prepare_batch(batch_id, "agent", 1)
    packages = {item["task_id"]: item["reading_package_id"] for item in prepared["results"]}
    citation_ids = {}
    for index, ref in enumerate(refs, 1):
        task_id = f"{batch_id}-task-{index}"
        task = service._task(task_id)
        common = {
            "task_id": task_id, "actor": "agent", "expected_revision": task["revision"],
            "reading_package_id": packages[task_id], "registry_revision": 1,
            "inspections": [{"source_ref": ref, "finding": f"resource {index} inspected",
                             "qa": "usable", "qa_note": ""}],
            "candidates": [{"candidate_id": f"pump-{index}", "name": "Pump X",
                            "kind": "entity", "identity_rationale": "same project pump",
                            "finding": f"resource {index} evidence", "applicability": "Project A",
                            "conditions": [], "exceptions": [], "support_refs": [ref]}],
            "empty_reason": "",
        }
        candidate = service.record_pass_batch({
            "batch_id": batch_id,
            "passes": [{**common, "pass_kind": "candidate", "sequence": 0}],
        })
        assert candidate["ok"]
        citation = service.record_pass_batch({
            "batch_id": batch_id,
            "passes": [{**common, "expected_revision": service._task(task_id)["revision"],
                        "pass_kind": "citation", "sequence": 1}],
        })
        assert citation["ok"]
        citation_ids[task_id] = citation["results"][0]["pass_id"]
    snapshots = [{"task_id": task_id, "revision": service._task(task_id)["revision"]}
                 for task_id in sorted(packages)]
    return service, refs, citation_ids, snapshots


def resource_reduce_requests(batch_id: str, citation_ids: dict[str, str],
                             snapshots: list[dict]) -> list[dict]:
    requests = []
    for index, snapshot in enumerate(snapshots, 1):
        candidate_ref = {"pass_id": citation_ids[snapshot["task_id"]],
                         "candidate_id": f"pump-{index}"}
        requests.append({
            "batch_id": batch_id, "resource_id": f"resource-{index}", "actor": "agent",
            "tasks": [snapshot],
            "proposals": [{
                "proposal_id": f"resource-{index}-pump", "candidate_refs": [candidate_ref],
                "summary": f"Resource {index} proposes the Project A pump identity",
                "identity_hints": [{"kind": "entity", "identity_key": "project-a:pump-x",
                                    "canonical_name": "Pump X", "aliases": []}],
                "path_hints": ["30_Cards/layered-pump-x.md"],
            }],
            "omitted_candidate_refs": [], "reason": "Resource-local candidate reduction",
        })
    return requests


def workflow_request(**fields: object) -> dict:
    request = dict(fields)
    request["input_digest"] = mutation_digest(request)
    return request


def test_workflow_ledger_adopts_batch_and_rebuilds_desired_nodes(vault: Path):
    batch = plan_sliced_batch(vault, 4, "workflow-batch")
    workflow = FileIngestWorkflowService(vault)
    start = workflow_request(
        workflow_id="ingest-workflow-batch", actor="agent", expected_revision=0,
        profile="compact-3", scope={"source_paths": [], "knowledge_selector": "all-current"},
        batch_id="workflow-batch")
    created = workflow.start(start)
    assert created["state"] == "analyzing" and created["revision"] == 1
    assert workflow.start(start) == created
    with pytest.raises(ContractError, match="IDEMPOTENCY_CONFLICT"):
        workflow.start(workflow_request(**{**start, "profile": "diagnostic-6"}))
    assert workflow.status("ingest-workflow-batch", compact=True)["batch_id"] == "workflow-batch"
    assert workflow.resume(workflow_request(
        workflow_id="ingest-workflow-batch", actor="agent", expected_revision=1)) == created
    batch.batch_next_slice("workflow-batch", "worker-a", now="2026-09-21T00:00:00Z")
    rebuilt = workflow.rebuild_kanban(workflow_request(
        workflow_id="ingest-workflow-batch", actor="agent", expected_revision=1))
    assert rebuilt["kanban"]["board_id"] is None
    assert len(rebuilt["kanban"]["task_map"]) == 2
    assert all(item["idempotency_key"].startswith("ingest:ingest-workflow-batch:pass-slice:")
               for item in rebuilt["kanban"]["task_map"])
    with pytest.raises(ContractError, match="REVISION_CONFLICT"):
        workflow.cancel(workflow_request(
            workflow_id="ingest-workflow-batch", actor="agent", expected_revision=1))
    cancelled = workflow.cancel(workflow_request(
        workflow_id="ingest-workflow-batch", actor="agent", expected_revision=2))
    assert cancelled["cancel_requested"] and cancelled["state"] == "cancelled"
    with pytest.raises(ContractError, match="WORKFLOW_STOPPED"):
        workflow.rebuild_kanban(workflow_request(
            workflow_id="ingest-workflow-batch", actor="agent", expected_revision=3))
    resumed = workflow.resume(workflow_request(
        workflow_id="ingest-workflow-batch", actor="agent", expected_revision=3))
    assert resumed["state"] == "analyzing" and not resumed["cancel_requested"]
    assert batch._batch("workflow-batch")["task_ids"]


def test_workflow_mutation_digest_and_checkpoint_gate(vault: Path):
    workflow = FileIngestWorkflowService(vault)
    created = workflow.start(workflow_request(
        workflow_id="ingest-gate", actor="agent", expected_revision=0,
        profile="diagnostic-6", scope={"source_paths": [],
                                        "knowledge_selector": "all-current"}))
    assert created["state"] == "created"
    with pytest.raises(ContractError, match="INPUT_DIGEST_MISMATCH"):
        workflow.reconcile({"workflow_id": "ingest-gate", "actor": "agent",
                            "expected_revision": 1, "target_stage": "source_preparing",
                            "input_digest": "sha256:" + "0" * 64})
    with pytest.raises(ContractError, match="INVALID_TRANSITION"):
        workflow.reconcile(workflow_request(
            workflow_id="ingest-gate", actor="agent", expected_revision=1,
            target_stage="applying"))
    advanced = workflow.reconcile(workflow_request(
        workflow_id="ingest-gate", actor="agent", expected_revision=1,
        target_stage="source_preparing"))
    assert advanced["revision"] == 2
    with pytest.raises(ContractError, match="INVALID_TRANSITION"):
        workflow.approve(workflow_request(
            workflow_id="ingest-gate", actor="agent", expected_revision=2,
            checkpoint="checkpoint_1", approval_digest="sha256:" + "0" * 64))
    planned = workflow.reconcile(workflow_request(
        workflow_id="ingest-gate", actor="agent", expected_revision=2,
        target_stage="planning"))
    assert planned["revision"] == 3
    with pytest.raises(ContractError, match="INVALID_SCHEMA"):
        workflow.reconcile(workflow_request(
            workflow_id="ingest-gate", actor="agent", expected_revision=3,
            target_stage="analyzing"))
    plan_sliced_batch(vault, 1, "fresh-workflow-batch")
    analyzing = workflow.reconcile(workflow_request(
        workflow_id="ingest-gate", actor="agent", expected_revision=3,
        target_stage="analyzing", batch_id="fresh-workflow-batch"))
    assert analyzing["batch_id"] == "fresh-workflow-batch"


def test_workflow_checkpoint_one_requires_validated_batch_and_explicit_digest(vault: Path):
    batch_id = "workflow-checkpoint"
    batch, _, citations, snapshots = prepare_layered_reduce_batch(vault, batch_id)
    workflow = FileIngestWorkflowService(vault)
    started = workflow.start(workflow_request(
        workflow_id="ingest-checkpoint", actor="agent", expected_revision=0,
        profile="compact-3", scope={"source_paths": [],
                                    "knowledge_selector": "all-current"},
        batch_id=batch_id))
    assert started["current_stage"] == "analyzing"
    reduced = workflow.reconcile(workflow_request(
        workflow_id="ingest-checkpoint", actor="agent", expected_revision=1,
        target_stage="reducing"))
    assert reduced["revision"] == 2
    with pytest.raises(ContractError, match="INCOMPLETE_COVERAGE"):
        workflow.reconcile(workflow_request(
            workflow_id="ingest-checkpoint", actor="agent", expected_revision=2,
            target_stage="checkpoint_1"))
    requests = resource_reduce_requests(batch_id, citations, snapshots)
    reductions = [batch.reduce_resource(item)["reduction"] for item in requests]
    candidate_refs = [item["proposals"][0]["candidate_refs"][0] for item in requests]
    batch.reduce_global({
        "batch_id": batch_id, "actor": "agent",
        "resource_reduction_ids": [item["reduction_id"] for item in reductions],
        "runs": [{"run_id": "workflow-checkpoint-run", "actor": "agent",
                  "tasks": snapshots, "expected_registry_revision": 0,
                  "document_registry_revision": 1,
                  "decisions": [{"candidate_refs": candidate_refs,
                                 "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                                              "canonical_name": "Pump X", "aliases": []},
                                 "action": "create", "path": "30_Cards/workflow-pump-x.md",
                                 "content": "# Pump X\n\nTransfers coolant.\n"}],
                  "reason": "Global identity coordination"}],
        "omitted_candidate_refs": [], "reason": "Global coordination",
    })
    assert batch.validate_batch(batch_id)["ok"]
    checkpoint = workflow.reconcile(workflow_request(
        workflow_id="ingest-checkpoint", actor="agent", expected_revision=2,
        target_stage="checkpoint_1"))
    digest = checkpoint["checkpoints"]["checkpoint_1"]["approval_digest"]
    with pytest.raises(ContractError, match="AWAITING_APPROVAL"):
        workflow.reconcile(workflow_request(
            workflow_id="ingest-checkpoint", actor="agent", expected_revision=3,
            target_stage="build_finalizing"))
    with pytest.raises(ContractError, match="APPROVAL_DIGEST_MISMATCH"):
        workflow.approve(workflow_request(
            workflow_id="ingest-checkpoint", actor="agent", expected_revision=3,
            checkpoint="checkpoint_1", approval_digest="sha256:" + "0" * 64))
    approved = workflow.approve(workflow_request(
        workflow_id="ingest-checkpoint", actor="agent", expected_revision=3,
        checkpoint="checkpoint_1", approval_digest=digest))
    assert approved["checkpoints"]["checkpoint_1"]["state"] == "approved"
    assert workflow.reconcile(workflow_request(
        workflow_id="ingest-checkpoint", actor="agent", expected_revision=4,
        target_stage="build_finalizing"))["state"] == "build_finalizing"


def test_workflow_cli_start_digest_and_compact_status(vault: Path, tmp_path: Path):
    request = {"workflow_id": "ingest-cli", "actor": "agent", "expected_revision": 0,
               "profile": "compact-3", "scope": {"source_paths": [],
                                               "knowledge_selector": "all-current"}}
    path = tmp_path / "workflow-request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    digest = subprocess.run([sys.executable, str(WORKFLOW_CLI), "--vault", str(vault),
                             "digest", "--request", str(path)], capture_output=True,
                            text=True, encoding="utf-8")
    assert digest.returncode == 0, digest.stderr
    request.update(json.loads(digest.stdout))
    path.write_text(json.dumps(request), encoding="utf-8")
    started = subprocess.run([sys.executable, str(WORKFLOW_CLI), "--vault", str(vault),
                              "start", "--request", str(path)], capture_output=True,
                             text=True, encoding="utf-8")
    assert started.returncode == 0, started.stderr
    status = subprocess.run([sys.executable, str(WORKFLOW_CLI), "--vault", str(vault),
                             "status", "--workflow-id", "ingest-cli", "--compact"],
                            capture_output=True, text=True, encoding="utf-8")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["state"] == "created"
    assert "artifacts" not in json.loads(status.stdout)


def test_slice_leases_are_deterministic_bounded_and_worker_exclusive(vault: Path):
    service = plan_sliced_batch(vault, 7)
    now = "2026-09-21T00:00:00Z"
    first = service.batch_next_slice("slice-batch", "worker-a", now=now)
    second = service.batch_next_slice("slice-batch", "worker-b", now=now)
    limited = service.batch_next_slice("slice-batch", "worker-c", now=now)
    assert first["leased"] and second["leased"]
    assert first["slice"]["slice_id"] != second["slice"]["slice_id"]
    assert len(first["slice"]["task_ids"]) == 3
    assert len(second["slice"]["task_ids"]) == 3
    assert limited == {"ok": True, "leased": False,
                       "reason": "concurrency_limit", "slice": None}

    heartbeat = service.slice_heartbeat(
        "slice-batch", first["slice"]["slice_id"], "worker-a",
        first["slice"]["revision"], now="2026-09-21T00:10:00Z")
    assert heartbeat["slice"]["lease"]["expires_at"] == "2026-09-21T00:40:00Z"
    with pytest.raises(ContractError, match="requires at least one result reference"):
        service.slice_complete({
            "batch_id": "slice-batch", "slice_id": first["slice"]["slice_id"],
            "worker_id": "worker-a", "expected_revision": heartbeat["slice"]["revision"],
            "result_refs": [],
        }, now="2026-09-21T00:10:01Z")
    completed = service.slice_complete({
        "batch_id": "slice-batch", "slice_id": first["slice"]["slice_id"],
        "worker_id": "worker-a", "expected_revision": heartbeat["slice"]["revision"],
        "result_refs": ["_system/knowledge-builds/task-result/pass.json"],
    }, now="2026-09-21T00:10:01Z")
    assert completed["slice"]["state"] == "completed"
    third = service.batch_next_slice("slice-batch", "worker-c", now=now)
    assert third["leased"] and len(third["slice"]["task_ids"]) == 1
    status = service.batch_status("slice-batch", compact=True)
    assert status["slice_counts"] == {"completed": 1, "leased": 2}
    assert status["coverage"]["total_slices"] == 3
    assert "slices" not in status


def test_expired_slice_lease_is_reclaimed_and_max_attempts_block(vault: Path):
    service = plan_sliced_batch(vault, 1, "expiry-batch")
    config = {"pass_worker_concurrency": 1, "slice_max_tasks": 1,
              "slice_max_input_codepoints": 30000, "lease_seconds": 10,
              "heartbeat_seconds": 5, "max_attempts": 2}
    first = service.batch_next_slice(
        "expiry-batch", "worker-a", config=config, now="2026-09-21T00:00:00Z")["slice"]
    with pytest.raises(ContractError, match="LEASE_EXPIRED"):
        service.slice_complete({
            "batch_id": "expiry-batch", "slice_id": first["slice_id"],
            "worker_id": "worker-a", "expected_revision": first["revision"],
            "result_refs": ["late-pass-result"],
        }, now="2026-09-21T00:00:11Z")
    reclaimed = service.reclaim_expired_slices("expiry-batch", now="2026-09-21T00:00:11Z")
    assert reclaimed["reclaimed"] == 1
    assert reclaimed["slices"][0]["state"] == "ready"
    second = service.batch_next_slice(
        "expiry-batch", "worker-b", now="2026-09-21T00:00:12Z")["slice"]
    assert second["slice_id"] == first["slice_id"] and second["attempt"] == 2
    exhausted = service.reclaim_expired_slices("expiry-batch", now="2026-09-21T00:00:23Z")
    assert exhausted["slices"][0]["state"] == "blocked"
    assert exhausted["slices"][0]["last_error"]["code"] == "LEASE_EXPIRED"


def test_slice_retry_wait_and_batch_cancel_preserve_completed_work(vault: Path):
    service = plan_sliced_batch(vault, 2, "cancel-batch")
    config = {"pass_worker_concurrency": 1, "slice_max_tasks": 1,
              "slice_max_input_codepoints": 30000, "lease_seconds": 1800,
              "heartbeat_seconds": 60, "max_attempts": 3}
    first = service.batch_next_slice(
        "cancel-batch", "worker-a", config=config, now="2026-09-21T00:00:00Z")["slice"]
    failed = service.slice_fail({
        "batch_id": "cancel-batch", "slice_id": first["slice_id"],
        "worker_id": "worker-a", "expected_revision": first["revision"],
        "code": "TIMEOUT", "message": "model timed out", "retryable": True,
        "retry_after_seconds": 60,
    }, now="2026-09-21T00:00:10Z")["slice"]
    assert failed["state"] == "retry_wait" and failed["retry_at"] == "2026-09-21T00:01:10Z"
    before_retry = service.batch_next_slice(
        "cancel-batch", "worker-b", now="2026-09-21T00:00:40Z")["slice"]
    assert before_retry["slice_id"] != first["slice_id"]
    completed = service.slice_complete({
        "batch_id": "cancel-batch", "slice_id": before_retry["slice_id"],
        "worker_id": "worker-b", "expected_revision": before_retry["revision"],
        "result_refs": ["pass-existing"],
    }, now="2026-09-21T00:00:41Z")["slice"]
    retry = service.batch_next_slice(
        "cancel-batch", "worker-c", now="2026-09-21T00:01:11Z")["slice"]
    assert retry["slice_id"] == first["slice_id"] and retry["attempt"] == 2
    batch_revision = service.batch_status("cancel-batch", compact=True)["batch"]["revision"]
    cancelled = service.cancel_batch("cancel-batch", "agent", batch_revision)
    assert cancelled["batch"]["state"] == "cancelled"
    assert cancelled["cancelled_slice_ids"] == [retry["slice_id"]]
    assert service._slice("cancel-batch", completed["slice_id"])["state"] == "completed"
    assert service.batch_next_slice("cancel-batch", "worker-d")["reason"] == "batch_cancelled"
    status = service.batch_status("cancel-batch", compact=True)
    assert status["next_actions"] == []
    with pytest.raises(ContractError, match="CANCELLED"):
        service.slice_fail({
            "batch_id": "cancel-batch", "slice_id": retry["slice_id"],
            "worker_id": "worker-c", "expected_revision": retry["revision"] + 1,
            "code": "LATE", "message": "late worker response",
        })


def test_batch_pass_idempotency_survives_worker_restart_and_rejects_key_conflict(vault: Path):
    service = plan_sliced_batch(vault, 1, "idempotent-pass")
    prepared = service.prepare_batch("idempotent-pass", "agent", 1)
    package_id = prepared["results"][0]["reading_package_id"]
    leased = service.batch_next_slice(
        "idempotent-pass", "worker-a", now="2026-09-21T00:00:00Z")["slice"]
    task_id = leased["task_ids"][0]
    task = service._task(task_id)
    ref = task["target_refs"][0]
    pass_request = {
        "task_id": task_id, "actor": "agent", "expected_revision": task["revision"],
        "reading_package_id": package_id, "registry_revision": 1,
        "pass_kind": "candidate", "sequence": 0,
        "inspections": [{"source_ref": ref, "finding": "bounded inspection",
                         "qa": "usable", "qa_note": ""}],
        "candidates": [], "empty_reason": "No reusable candidate",
    }
    batch_request = {"batch_id": "idempotent-pass", "slice_id": leased["slice_id"],
                     "worker_id": "worker-a", "passes": [pass_request]}
    first = service.record_pass_batch(batch_request)
    assert first["ok"] and first["results"][0]["created"] is True
    assert first["results"][0]["idempotency_key"].startswith("sha256:")
    stored_path = vault / first["results"][0]["path"]
    stored_hash = hashlib.sha256(stored_path.read_bytes()).hexdigest()

    retried = service.record_pass_batch(batch_request)
    assert retried["ok"] and retried["results"][0]["created"] is False
    assert retried["results"][0]["pass_id"] == first["results"][0]["pass_id"]
    assert hashlib.sha256(stored_path.read_bytes()).hexdigest() == stored_hash

    conflicting = json.loads(json.dumps(batch_request))
    conflicting["passes"][0]["empty_reason"] = "Different semantic result"
    conflict = service.record_pass_batch(conflicting)
    assert conflict["ok"] is False
    assert conflict["failures"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    completed = service.slice_complete({
        "batch_id": "idempotent-pass", "slice_id": leased["slice_id"],
        "worker_id": "worker-a", "expected_revision": leased["revision"],
        "result_refs": [retried["results"][0]["path"]],
    }, now="2026-09-21T00:00:10Z")
    assert completed["slice"]["state"] == "completed"


def test_batch_pass_rejects_stale_revision_before_writing(vault: Path):
    service = plan_sliced_batch(vault, 1, "stale-pass")
    prepared = service.prepare_batch("stale-pass", "agent", 1)
    leased = service.batch_next_slice(
        "stale-pass", "worker-a", now="2026-09-21T00:00:00Z")["slice"]
    task_id = leased["task_ids"][0]
    task = service._task(task_id)
    result = service.record_pass_batch({
        "batch_id": "stale-pass", "slice_id": leased["slice_id"],
        "worker_id": "worker-a", "passes": [{
            "task_id": task_id, "actor": "agent",
            "expected_revision": task["revision"] - 1,
            "reading_package_id": prepared["results"][0]["reading_package_id"],
            "registry_revision": 1, "pass_kind": "candidate", "sequence": 0,
            "inspections": [{"source_ref": task["target_refs"][0],
                             "finding": "stale inspection", "qa": "usable",
                             "qa_note": ""}],
            "candidates": [], "empty_reason": "No candidate",
        }],
    })
    assert result["ok"] is False and result["failures"][0]["code"] == "STALE_INPUT"
    assert not (vault / f"_system/knowledge-builds/task-{task_id}/passes").exists()


def test_slice_failure_policy_persists_output_and_applies_batch_cooldown(vault: Path):
    service = plan_sliced_batch(vault, 2, "failure-policy")
    config = {"pass_worker_concurrency": 2, "slice_max_tasks": 1,
              "slice_max_input_codepoints": 30000, "lease_seconds": 1800,
              "heartbeat_seconds": 60, "max_attempts": 3}
    rate_limited = service.batch_next_slice(
        "failure-policy", "worker-a", config=config,
        now="2026-09-21T00:00:00Z")["slice"]
    failed = service.slice_fail({
        "batch_id": "failure-policy", "slice_id": rate_limited["slice_id"],
        "worker_id": "worker-a", "expected_revision": rate_limited["revision"],
        "code": "HTTP_429", "message": "provider rate limit",
    }, now="2026-09-21T00:00:01Z")
    assert failed["slice"]["state"] == "retry_wait"
    assert failed["slice"]["retry_at"] == "2026-09-21T00:01:01Z"
    assert failed["batch"]["cooldown_until"] == "2026-09-21T00:01:01Z"
    cooled = service.batch_next_slice(
        "failure-policy", "worker-b", now="2026-09-21T00:00:30Z")
    assert cooled["reason"] == "batch_cooldown"

    invalid = service.batch_next_slice(
        "failure-policy", "worker-b", now="2026-09-21T00:01:02Z")["slice"]
    invalid_result = service.slice_fail({
        "batch_id": "failure-policy", "slice_id": invalid["slice_id"],
        "worker_id": "worker-b", "expected_revision": invalid["revision"],
        "code": "INVALID_MODEL_JSON", "message": "trailing prose",
        "failed_output": "{not-json}\nmodel explanation",
    }, now="2026-09-21T00:01:03Z")["slice"]
    assert invalid_result["state"] == "retry_wait"
    output_ref = invalid_result["last_error"]["output_ref"]
    assert (vault / output_ref).read_text(encoding="utf-8") == "{not-json}\nmodel explanation"


def test_slice_failure_policy_routes_stale_budget_and_checkpoint_states(vault: Path):
    cases = [
        ("STALE_INPUT", "reconcile_required", "reconcile", 0),
        ("READING_BUDGET_MISMATCH", "reconcile_required", "remeasure_and_reslice", 1),
        ("WHOLE_ASSET_OVERSIZE", "blocked", "blocked", 0),
        ("PROVENANCE_ERROR", "blocked", "blocked", 0),
        ("AWAITING_APPROVAL", "awaiting_approval", "await_approval", 0),
    ]
    service = plan_sliced_batch(vault, len(cases), "failure-classes")
    config = {"pass_worker_concurrency": 1, "slice_max_tasks": 1,
              "slice_max_input_codepoints": 30000, "lease_seconds": 1800,
              "heartbeat_seconds": 60, "max_attempts": 3}
    for index, (code, state, action, reslice_count) in enumerate(cases, 1):
        leased = service.batch_next_slice(
            "failure-classes", f"worker-{index}", config=config if index == 1 else None,
            now="2026-09-21T00:00:00Z")["slice"]
        result = service.slice_fail({
            "batch_id": "failure-classes", "slice_id": leased["slice_id"],
            "worker_id": f"worker-{index}", "expected_revision": leased["revision"],
            "code": code, "message": "classified failure",
        }, now="2026-09-21T00:00:01Z")["slice"]
        assert result["state"] == state
        assert result["last_error"]["action"] == action
        assert result["reslice_count"] == reslice_count


def test_reading_budget_failure_allows_exactly_one_deterministic_reslice(vault: Path):
    service = plan_sliced_batch(vault, 1, "reslice-once")
    first = service.batch_next_slice(
        "reslice-once", "worker-a", now="2026-09-21T00:00:00Z")["slice"]
    failed = service.slice_fail({
        "batch_id": "reslice-once", "slice_id": first["slice_id"],
        "worker_id": "worker-a", "expected_revision": first["revision"],
        "code": "READING_BUDGET_MISMATCH", "message": "measured input drifted",
    }, now="2026-09-21T00:00:01Z")["slice"]
    reconciled = service.reconcile_slice_inputs(
        "reslice-once", first["slice_id"], "agent", failed["revision"])
    assert reconciled["superseded"]["state"] == "cancelled"
    assert len(reconciled["replacements"]) == 1
    replacement = reconciled["replacements"][0]
    assert replacement["state"] == "ready" and replacement["reslice_count"] == 1
    assert replacement["slice_id"] != first["slice_id"]

    leased_again = service.batch_next_slice(
        "reslice-once", "worker-b", now="2026-09-21T00:00:02Z")["slice"]
    exhausted = service.slice_fail({
        "batch_id": "reslice-once", "slice_id": leased_again["slice_id"],
        "worker_id": "worker-b", "expected_revision": leased_again["revision"],
        "code": "READING_BUDGET_MISMATCH", "message": "still too large",
    }, now="2026-09-21T00:00:03Z")["slice"]
    assert exhausted["state"] == "blocked"
    assert exhausted["reslice_count"] == 1


def test_layered_reduce_is_idempotent_and_materializes_global_draft(vault: Path):
    batch_id = "layered-reduce"
    service, refs, citation_ids, snapshots = prepare_layered_reduce_batch(vault, batch_id)
    requests = resource_reduce_requests(batch_id, citation_ids, snapshots)
    reductions = [service.reduce_resource(request) for request in requests]
    assert all(item["created"] for item in reductions)
    first_record = reductions[0]["reduction"]
    assert "reading_package" not in json.dumps(first_record)
    assert first_record["pass_refs"][0]["task_id"] == snapshots[0]["task_id"]
    assert service.reduce_resource(requests[0])["created"] is False
    changed = json.loads(json.dumps(requests[0]))
    changed["proposals"][0]["summary"] = "Conflicting reducer output"
    with pytest.raises(ContractError, match="IDEMPOTENCY_CONFLICT"):
        service.reduce_resource(changed)

    candidate_refs = [request["proposals"][0]["candidate_refs"][0] for request in requests]
    global_request = {
        "batch_id": batch_id, "actor": "agent",
        "resource_reduction_ids": [item["reduction"]["reduction_id"] for item in reductions],
        "runs": [{
            "run_id": "layered-run", "actor": "agent", "tasks": snapshots,
            "expected_registry_revision": 0, "document_registry_revision": 1,
            "decisions": [{
                "candidate_refs": candidate_refs,
                "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                             "canonical_name": "Pump X", "aliases": []},
                "action": "create", "path": "30_Cards/layered-pump-x.md",
                "content": "# Pump X\n\nTransfers coolant and requires filtered fluid.\n",
            }], "reason": "Global identity and path coordination",
        }],
        "omitted_candidate_refs": [], "reason": "Coordinated both resource proposals",
    }
    coordinated = service.reduce_global(global_request)
    assert coordinated["created"] is True
    assert coordinated["coordination"]["run_ids"] == ["layered-run"]
    assert (vault / "_system/knowledge-builds/layered-run/manifest.json").is_file()
    assert service.reduce_global(global_request)["created"] is False
    status = service.batch_status(batch_id, compact=True)
    assert status["coverage"]["resource_reductions"] == 2
    assert status["coverage"]["global_reduction_complete"] is True


def test_global_reduce_rejects_unproposed_candidates_and_duplicate_task_ownership(vault: Path):
    batch_id = "layered-guards"
    service, _, citation_ids, snapshots = prepare_layered_reduce_batch(vault, batch_id)
    requests = resource_reduce_requests(batch_id, citation_ids, snapshots)
    reductions = [service.reduce_resource(request) for request in requests]
    candidate_refs = [request["proposals"][0]["candidate_refs"][0] for request in requests]
    base = {
        "batch_id": batch_id, "actor": "agent",
        "resource_reduction_ids": [item["reduction"]["reduction_id"] for item in reductions],
        "runs": [{
            "run_id": "guard-run-a", "actor": "agent", "tasks": snapshots,
            "expected_registry_revision": 0, "document_registry_revision": 1,
            "decisions": [{
                "candidate_refs": candidate_refs,
                "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                             "canonical_name": "Pump X", "aliases": []},
                "action": "create", "path": "30_Cards/guard-pump-x.md",
                "content": "# Pump X\n\nGuarded global output.\n",
            }], "reason": "Guard test",
        }], "omitted_candidate_refs": [], "reason": "Guard coordination",
    }
    outside = json.loads(json.dumps(base))
    outside["runs"][0]["decisions"][0]["candidate_refs"][0]["candidate_id"] = "not-proposed"
    with pytest.raises(ContractError, match="use or omit every resource proposal candidate"):
        service.reduce_global(outside)

    duplicate = json.loads(json.dumps(base))
    duplicate["runs"].append({**duplicate["runs"][0], "run_id": "guard-run-b",
                              "decisions": []})
    with pytest.raises(ContractError, match="multiple draft runs"):
        service.reduce_global(duplicate)

    conflicting = json.loads(json.dumps(base))
    conflicting["runs"] = []
    for index, snapshot in enumerate(snapshots):
        conflicting["runs"].append({
            "run_id": f"conflict-run-{index + 1}", "actor": "agent", "tasks": [snapshot],
            "expected_registry_revision": 0, "document_registry_revision": 1,
            "decisions": [{
                "candidate_refs": [candidate_refs[index]],
                "identity": {"kind": "entity", "identity_key": "project-a:pump-x",
                             "canonical_name": "Pump X", "aliases": []},
                "action": "create", "path": "30_Cards/conflicting-pump.md",
                "content": "# Pump X\n\nConflicting ownership.\n",
            }], "reason": "Conflict guard",
        })
    with pytest.raises(ContractError, match="path .*multiple runs"):
        service.reduce_global(conflicting)


def test_p3_cli_runs_from_embedded_skill_runtime(vault: Path):
    result = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                             "--vault", str(vault), "identities"],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["registry"]["subjects"] == []

    ref = publish_sources(vault, ["# CLI batch\nEvidence.\n"])[0]
    request = vault / "_system/reports/cli-batch-plan.json"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(json.dumps({
        "batch_id": "cli-batch", "actor": "agent", "registry_revision": 1,
        "tasks": [{"task_id": "cli-batch-task", "target_refs": [ref],
                   "expected_revision": 0}],
    }), encoding="utf-8")
    measured = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                               "--vault", str(vault), "batch-measure", "--request", str(request)],
                              capture_output=True, text=True, encoding="utf-8")
    assert measured.returncode == 0, measured.stdout + measured.stderr
    assert json.loads(measured.stdout)["results"][0]["measurement"]["fits"] is True
    planned = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                              "--vault", str(vault), "batch-plan", "--request", str(request),
                              "--exact-reading-budget"],
                             capture_output=True, text=True, encoding="utf-8")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert json.loads(planned.stdout)["batch"]["task_ids"] == ["cli-batch-task"]
    status = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                             "--vault", str(vault), "batch-status", "--batch-id", "cli-batch",
                             "--compact"],
                            capture_output=True, text=True, encoding="utf-8")
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)["task_counts"] == {"pending": 1}
    assert "tasks" not in json.loads(status.stdout)
    assert json.loads(status.stdout)["coverage"]["total_tasks"] == 1
    leased = subprocess.run([sys.executable, "-I", "-S", str(KNOWLEDGE_CLI),
                             "--vault", str(vault), "batch-next-slice",
                             "--batch-id", "cli-batch", "--worker-id", "cli-worker"],
                            capture_output=True, text=True, encoding="utf-8")
    assert leased.returncode == 0, leased.stdout + leased.stderr
    leased_slice = json.loads(leased.stdout)["slice"]
    heartbeat = subprocess.run([
        sys.executable, "-I", "-S", str(KNOWLEDGE_CLI), "--vault", str(vault),
        "slice-heartbeat", "--batch-id", "cli-batch", "--slice-id", leased_slice["slice_id"],
        "--worker-id", "cli-worker", "--expected-revision", str(leased_slice["revision"]),
    ], capture_output=True, text=True, encoding="utf-8")
    assert heartbeat.returncode == 0, heartbeat.stdout + heartbeat.stderr
    heartbeat_slice = json.loads(heartbeat.stdout)["slice"]
    complete_request = vault / "_system/reports/cli-slice-complete.json"
    complete_request.write_text(json.dumps({
        "batch_id": "cli-batch", "slice_id": leased_slice["slice_id"],
        "worker_id": "cli-worker", "expected_revision": heartbeat_slice["revision"],
        "result_refs": ["cli-pass-result"],
    }), encoding="utf-8")
    completed = subprocess.run([
        sys.executable, "-I", "-S", str(KNOWLEDGE_CLI), "--vault", str(vault),
        "slice-complete", "--request", str(complete_request),
    ], capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["slice"]["state"] == "completed"


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
