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
