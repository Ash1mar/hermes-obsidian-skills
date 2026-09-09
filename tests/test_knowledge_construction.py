from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes-obsidian-controlled-ingest/scripts/validate_knowledge_build.py"
spec = importlib.util.spec_from_file_location("build_validator", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "10_Raw/source.md"
    source.parent.mkdir()
    source.write_text("Object A has a documented function.\nIt applies to project X.\n", encoding="utf-8")
    output = tmp_path / "30_Cards/object.md"
    output.parent.mkdir()
    output.write_text("# Object A\n", encoding="utf-8")
    data = {
        "contract": validator.PREVIOUS_CONTRACT, "scope": "source.md lines 1-2",
        "execution_status": "completed",
        "candidates": [{"id": "candidate-001", "name": "Object A", "kind": "entity",
                        "identity_rationale": "Project X; no equivalent target found",
                        "decision": "create", "reason": "Reusable object description",
                        "existing_targets": [], "outputs": ["30_Cards/object.md"],
                        "evidence": [{"path": "10_Raw/source.md", "lines": [1, 2],
                                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                      "qa": "usable"}]}]}
    data["inspected_ranges"] = [dict(data["candidates"][0]["evidence"][0], reason="Read object function and applicability")]
    return tmp_path, data


def test_complete_and_plan(sample):
    vault, data = sample
    assert validator.validate_record(data, vault) == []
    assert any("already exists" in e for e in validator.validate_record(data, vault, "plan"))
    data["candidates"][0]["outputs"] = ["30_Cards/new.md"]
    assert validator.validate_record(data, vault, "plan") == []
    assert any("missing output" in e for e in validator.validate_record(data, vault))


@pytest.mark.parametrize("mutation,expected", [
    (lambda c: c["evidence"][0].update(lines=[0, 2]), "line range"),
    (lambda c: c["evidence"][0].update(lines=[1, 99]), "exceeds"),
    (lambda c: c["evidence"][0].update(qa="needs-qa"), "QA-restricted"),
    (lambda c: c["evidence"][0].update(path="../outside.md"), "unsafe path"),
    (lambda c: c.update(outputs=["10_Raw/overwrite.md"]), "derived knowledge"),
    (lambda c: c.update(kind="analysis"), "derivation"),
    (lambda c: c.update(decision="reuse"), "inspection required"),
    (lambda c: c.update(evidence=[]), "supporting evidence"),
    (lambda c: c.update(decision="defer"), "cannot claim"),
])
def test_invalid_decisions(sample, mutation, expected):
    vault, data = sample
    mutation(data["candidates"][0])
    assert any(expected in e for e in validator.validate_record(data, vault))


def test_changed_source_and_duplicate_ids(sample):
    vault, data = sample
    (vault / "10_Raw/source.md").write_text("Changed\n", encoding="utf-8")
    data["candidates"].append(copy.deepcopy(data["candidates"][0]))
    errors = validator.validate_record(data, vault)
    assert any("fingerprint changed" in e for e in errors)
    assert any("duplicate" in e for e in errors)


def test_reuse_and_empty(sample):
    vault, data = sample
    c = data["candidates"][0]
    c.update(decision="reuse", existing_targets=c["outputs"])
    assert validator.validate_record(data, vault) == []
    data["candidates"] = []
    assert validator.validate_record(data, vault)
    data["empty_reason"] = "No usable content; no filename guesses"
    assert validator.validate_record(data, vault) == []


def test_cli_and_lint_integration(sample):
    vault, data = sample
    record = vault / "_system/reports/run.knowledge-build.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(data), encoding="utf-8")
    before = record.read_bytes()
    completed = subprocess.run([sys.executable, str(SCRIPT), str(record), "--vault", str(vault)], capture_output=True)
    assert completed.returncode == 0
    lint = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"
    def run_lint():
        result = subprocess.run([sys.executable, str(lint), "--vault", str(vault), "--json"], capture_output=True)
        return json.loads(result.stdout)
    result = run_lint()
    assert result["metrics"]["knowledge_build_records"] == 1
    assert not any(i["code"].startswith("knowledge_build.") for i in result["issues"])
    assert record.read_bytes() == before
    (vault / "10_Raw/source.md").write_text("Changed", encoding="utf-8")
    assert any(i["code"] == "knowledge_build.invalid" for i in run_lint()["issues"])
    record.unlink()
    result = run_lint()
    assert result["metrics"]["knowledge_build_records"] == 0
    assert not any(i["code"].startswith("knowledge_build.") for i in result["issues"])


def test_malformed_record(sample):
    vault, _ = sample
    record = vault / "broken.json"
    record.write_text("{", encoding="utf-8")
    assert validator.validate_file(record, vault)


def test_empty_requires_verifiable_inspection(sample):
    vault, data = sample
    data.update(candidates=[], empty_reason="No independent knowledge value")
    assert validator.validate_record(data, vault) == []
    data["inspected_ranges"][0]["sha256"] = "0" * 64
    assert any("fingerprint" in e for e in validator.validate_record(data, vault))
    data["inspected_ranges"] = []
    assert any("actual source inspection" in e for e in validator.validate_record(data, vault))


@pytest.mark.parametrize("status", ["not_started", "blocked", "in_progress"])
def test_unfinished_cannot_complete(sample, status):
    vault, data = sample
    data.update(execution_status=status, candidates=[], empty_reason="Unfinished")
    assert any("not completed" in e for e in validator.validate_record(data, vault))


def test_legacy_compatible_but_not_current_work(sample):
    vault, data = sample
    data = {"contract": validator.LEGACY_CONTRACT, "scope": "Historical run",
            "candidates": [], "empty_reason": "Old source-only report"}
    assert validator.validate_record(data, vault) == []
    record = vault / "old.knowledge-build.json"
    record.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(record), "--vault", str(vault),
                             "--require-current"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "current contract" in result.stdout


def test_bundle_inspection_and_all_supporting_output_registration(sample):
    vault, data = sample
    bundle = vault / "10_Raw/converted/example_document_bundle"
    bundle.mkdir(parents=True)
    source = bundle / "document.md"
    source.write_bytes((vault / "10_Raw/source.md").read_bytes())
    bundle_id = "bundle-v2-" + "a" * 16
    (bundle / "manifest.json").write_text(json.dumps({"source": {"sha256": "a" * 64}}), encoding="utf-8")
    item = data["inspected_ranges"][0]
    item.update(path=source.relative_to(vault).as_posix(), ledger_path="_system/reports/test.section-ledger.json",
                ledger_revision=2, bundle_id=bundle_id, section_id="section-1")
    data["candidates"][0]["evidence"][0]["path"] = item["path"]
    ledger = {"bundle_id": bundle_id, "revision": 3,
              "bundle": {"document_sha256": item["sha256"]},
              "sections": [{"id": "section-1", "status": "pending", "outputs": [],
                            "content_ranges": [{"start_line": 1, "end_line": 2}]}]}
    ledger_path = vault / item["ledger_path"]
    ledger_path.parent.mkdir(parents=True)

    def check():
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        return validator.validate_record(data, vault)

    assert any("not terminal" in e for e in check())
    ledger["sections"][0]["status"] = "ingested"
    assert any("output missing" in e for e in check())
    ledger["sections"][0]["outputs"] = data["candidates"][0]["outputs"]
    assert check() == []
    item["lines"] = [2, 2]
    assert any("outside inspected_ranges" in e for e in check())
    item["lines"] = [1, 2]
    ledger["sections"][0]["content_ranges"] = [{"start_line": 1, "end_line": 1}]
    assert any("outside section content_ranges" in e for e in check())


def run_sync(vault, data, apply=False):
    record = vault / "build.knowledge-build.json"
    record.write_text(json.dumps(data), encoding="utf-8")
    command = [sys.executable, str(SCRIPT.with_name("sync_knowledge_provenance.py")), str(record), "--vault", str(vault)]
    result = subprocess.run(command + (["--apply"] if apply else []), capture_output=True, text=True, encoding="utf-8")
    return result, json.loads(result.stdout)


def prepare_current(sample, qa=False):
    vault, data = sample
    data["contract"] = validator.CONTRACT
    (vault / "30_Cards/object.md").write_text("---\nstatus: draft\n---\n# Object A\nSource says A applies to X.\n", encoding="utf-8")
    if qa:
        data["candidates"][0]["evidence"][0].update(qa="needs-qa", qa_note="Unit symbol uncertain; retain as source transcription")
        data["inspected_ranges"][0].update(qa="needs-qa", qa_note="Unit symbol uncertain; retain as source transcription")
    result, preview = run_sync(vault, data)
    assert result.returncode == 0, preview
    data["output_reviews"] = [dict(row, finding="Compared attribution, scope, QA qualifiers and citations to the source")
                              for row in preview["review_inputs"]]
    return vault, data


@pytest.mark.parametrize("qa", [False, True])
def test_current_provenance_sync_is_idempotent_and_binds_final_review(sample, qa):
    vault, data = prepare_current(sample, qa)
    result, payload = run_sync(vault, data, True)
    assert result.returncode == 0, payload
    assert validator.validate_record(data, vault) == []
    page = vault / "30_Cards/object.md"
    before = page.read_bytes()
    assert run_sync(vault, data, True)[0].returncode == 0
    assert page.read_bytes() == before
    if qa:
        assert "待核验草稿" in page.read_text(encoding="utf-8")
    page.write_text(page.read_text(encoding="utf-8").replace("Source says", "Universal rule says"), encoding="utf-8")
    assert any("page changed after review" in e for e in validator.validate_record(data, vault))
    assert run_sync(vault, data, True)[0].returncode == 2


def test_shared_output_unions_all_candidates_and_detects_drift(sample):
    vault, data = prepare_current(sample)
    candidate = copy.deepcopy(data["candidates"][0])
    candidate.update(id="candidate-002", name="Second claim")
    candidate["evidence"][0]["lines"] = [2, 2]
    data["candidates"].append(candidate)
    assert run_sync(vault, data, True)[0].returncode == 0
    assert len(validator.output_provenance(data)["30_Cards/object.md"]) == 2
    assert validator.validate_record(data, vault) == []
    data["candidates"].pop()
    assert any("provenance differs" in e for e in validator.validate_record(data, vault))


def test_qa_cannot_be_laundered_as_verified_or_modify_nondraft(sample):
    vault, data = prepare_current(sample, True)
    page = vault / "30_Cards/object.md"
    page.write_text(page.read_text(encoding="utf-8").replace("status: draft", "status: approved"), encoding="utf-8")
    assert any("status: draft" in e for e in validator.validate_record(data, vault))
    assert run_sync(vault, data, True)[0].returncode == 2
    data["candidates"][0]["evidence"][0]["qa"] = "usable"
    assert any("unresolved QA" in e for e in validator.validate_record(data, vault))


def test_project_basis_is_required_without_asking_a_human(sample):
    vault, data = prepare_current(sample)
    target = vault / "50_Projects/object.md"
    target.parent.mkdir()
    target.write_bytes((vault / "30_Cards/object.md").read_bytes())
    data["candidates"][0]["outputs"] = ["50_Projects/object.md"]
    data["output_reviews"][0]["path"] = "50_Projects/object.md"
    assert any("project basis required" in e for e in validator.validate_record(data, vault))
    data["output_reviews"][0]["project_basis"] = "Source explicitly identifies project X"
    assert run_sync(vault, data, True)[0].returncode == 0
    assert validator.validate_record(data, vault) == []
