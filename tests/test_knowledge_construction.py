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
    return tmp_path, {
        "contract": validator.CONTRACT, "scope": "source.md lines 1-2",
        "candidates": [{"id": "candidate-001", "name": "Object A", "kind": "entity",
                        "identity_rationale": "Project X; no equivalent target found",
                        "decision": "create", "reason": "Reusable object description",
                        "existing_targets": [], "outputs": ["30_Cards/object.md"],
                        "evidence": [{"path": "10_Raw/source.md", "lines": [1, 2],
                                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                      "qa": "usable"}]}]}


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
