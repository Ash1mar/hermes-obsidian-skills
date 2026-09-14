"""P0 schema and evidence-relationship acceptance tests, not a splitter test suite."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "hermes-source-units"
sys.path.insert(0, str(PACKAGE / "src"))
from hermes_source_units.validation import (
    ContractError, canonical_json, check_schema, default_config, fingerprint, load_schema,
    validate_record, validate_references,
)


def example(name):
    return json.loads((PACKAGE / "examples" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind,name", [
    ("artifact", "artifact.json"), ("section", "section.json"),
    ("unit_set", "unit-set.json"), ("unit", "unit.json"),
    ("config", "config.json"), ("reading_window", "reading-window.json"),
    ("retrieval_window", "retrieval-window.json"), ("work", "work.json"),
    ("knowledge_build", "knowledge-build.json"),
    ("provider_extension", "provider-extension.json"),
])
def test_valid_contracts(kind, name):
    validate_references(kind, example(name), example("units.json"))


def test_schema_is_valid_and_self_contained():
    schema = load_schema()
    check_schema(schema)
    def check(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/$defs/")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(schema)


@pytest.mark.parametrize("case", example("invalid/cases.json"), ids=lambda c: c["file"])
def test_negative_fixture_rejected(case):
    with pytest.raises(ContractError) as raised:
        record = example(case["file"])
        if case["resolve"]:
            validate_references(case["kind"], record, example("units.json"))
        else:
            validate_record(case["kind"], record)
    assert raised.value.code == case["expected_code"]


def test_sample_positions_hashes_and_coverage_are_real():
    # Hand-authored oracle: proves this fixture is not merely arbitrary JSON.
    text = (PACKAGE / "examples/source/document.md").read_text(encoding="utf-8")
    assert "🧪" in text and len(text.encode("utf-16-le")) // 2 != len(text)
    units = example("units.json")
    cursor = 0
    for unit in units:
        loc = unit["locator"]
        if loc["kind"] == "asset":
            content = (PACKAGE / "examples/source" / loc["path"]).read_bytes()
        else:
            lo, hi = loc["span"]["start"], loc["span"]["end"]
            assert lo == cursor
            cursor = hi
            content = text[lo:hi].encode("utf-8")
            assert loc["line_start"] == text.count("\n", 0, lo) + 1
            assert loc["line_end"] == text.count("\n", 0, hi-1) + 1
        assert hashlib.sha256(content).hexdigest() == unit["content_sha256"]
    assert cursor == len(text)
    assert hashlib.sha256(text.encode()).hexdigest() == example("artifact.json")["document_sha256"]


def test_core_and_context_do_not_double_complete_work():
    record = example("work.json")
    record["inspections"] = record["inspections"][:1]
    with pytest.raises(ContractError, match="INCOMPLETE_COVERAGE"):
        validate_references("work", record, example("units.json"))


def test_adjacent_inspection_spans_cover_one_target_but_gap_does_not():
    record = example("work.json")
    source = record["target_refs"][0]
    record["target_refs"] = [source]
    hi = example("units.json")[0]["locator"]["span"]["end"]
    left, right = copy.deepcopy(record["inspections"][0]), copy.deepcopy(record["inspections"][0])
    left["source_ref"]["span"] = {"start": 0, "end": 10}
    right["source_ref"]["span"] = {"start": 10, "end": hi}
    record["inspections"] = [left, right]
    validate_references("work", record, example("units.json"))
    right["source_ref"]["span"]["start"] = 11
    with pytest.raises(ContractError, match="INCOMPLETE_COVERAGE"):
        validate_references("work", record, example("units.json"))


def test_qa_restriction_survives_into_output():
    record = example("knowledge-build.json")
    record["inspections"][2].update(qa="needs-qa", qa_note="Verify table source")
    validate_references("knowledge_build", record, example("units.json"))
    record["outputs"][0]["status"] = "reviewed"
    with pytest.raises(ContractError, match="QA_REQUIRES_DRAFT"):
        validate_references("knowledge_build", record, example("units.json"))


def test_multiple_sources_allowed_for_reading_not_retrieval():
    units = example("units.json")
    second = copy.deepcopy(units[0])
    second["ref"]["resource_id"] = "another-resource"
    second["prev_ref"] = second["next_ref"] = None
    units.append(second)
    for kind, name in (("reading_window", "reading-window.json"), ("retrieval_window", "retrieval-window.json")):
        record = example(name)
        record["context_refs"] = [{"unit_ref": second["ref"], "span": None}]
        if kind == "reading_window":
            validate_references(kind, record, units)
        else:
            with pytest.raises(ContractError, match="MIXED_SOURCE"):
                validate_references(kind, record, units)


@pytest.mark.parametrize("path", ["../x", "/x", "C:/x", "C:\\x", "a//b", "a/./b", "a/../b", "a\x00b", "a/b."])
def test_portable_path_boundary(path):
    record = example("unit.json")
    record["locator"]["path"] = path
    with pytest.raises(ContractError, match="INVALID_SCHEMA"):
        validate_record("unit", record)


def test_unknown_field_cannot_smuggle_content_authority():
    record = example("unit.json")
    record["indexed"] = True
    with pytest.raises(ContractError, match="INVALID_SCHEMA"):
        validate_record("unit", record)


def test_asset_refs_require_whole_asset_and_no_fake_text_offsets():
    units = example("units.json")
    record = example("provider-extension.json")
    record["source_unit_refs"] = [{"unit_ref": units[2]["ref"], "span": {"start": 0, "end": 1}}]
    with pytest.raises(ContractError, match="INVALID_RANGE"):
        validate_references("provider_extension", record, units)


def test_duplicate_unit_rejected_and_source_set_membership_checked():
    units = example("units.json")
    with pytest.raises(ContractError, match="DUPLICATE"):
        validate_references("unit_set", example("unit-set.json"), units + units[:1])
    units[0]["version_id"] = "different-version"
    with pytest.raises(ContractError, match="UNIT_SET_MISMATCH"):
        validate_references("unit_set", example("unit-set.json"), units)


def test_zero_candidate_completion_has_reason_and_real_inspection():
    record = example("knowledge-build.json")
    record.update(candidates=[], outputs=[], reason="Read completely; no reusable knowledge")
    validate_references("knowledge_build", record, example("units.json"))
    record["reason"] = ""
    with pytest.raises(ContractError, match="REASON_REQUIRED"):
        validate_record("knowledge_build", record)


def test_canonical_hash_is_unambiguous_and_model_config_separate():
    assert fingerprint({"a": "中", "b": 2}) == fingerprint({"b": 2, "a": "中"})
    assert fingerprint(["a|b", "c"]) != fingerprint(["a", "b|c"])
    assert fingerprint("e\u0301") != fingerprint("é")  # no hidden normalization
    config = default_config()
    source_fingerprint = fingerprint(config["source"])
    config["retrieval"]["max_tokens"] = 2048
    assert fingerprint(config["source"]) == source_fingerprint
    assert default_config()["retrieval"]["max_tokens"] == 1024
    assert default_config()["retrieval"]["tokenizer"] is None
    for item in (1.1, float("nan"), {1: "x"}, "\ud800"):
        with pytest.raises(ContractError, match="INVALID_CANONICAL_VALUE"):
            canonical_json(item)


def test_cli_is_read_only_and_reports_validation_limits():
    env = dict(os.environ, PYTHONPATH=str(PACKAGE / "src"))
    command = [sys.executable, "-m", "hermes_source_units", "knowledge_build", str(PACKAGE / "examples/knowledge-build.json"), "--units", str(PACKAGE / "examples/units.json")]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] and not data["source_integrity_verified"] and not data["authorization_verified"]
    result = subprocess.run([*command[:3], "unit", str(PACKAGE / "examples/invalid/missing-unit-id.json")], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "INVALID_SCHEMA"


@pytest.mark.parametrize("mutate", [
    lambda s: s["$defs"]["unit"].update(unevaluatedProperties=False),
    lambda s: s["$defs"]["unit"].update(required=["does-not-exist"]),
    lambda s: s["$defs"].update(loop={"$ref": "#/$defs/loop"}),
    lambda s: s["$defs"].update(remote={"$ref": "https://example.invalid/schema"}),
    lambda s: s["$defs"]["unit_ref"]["properties"]["vault_id"].update(format="unknown-format"),
    lambda s: s["$defs"]["unit_ref"]["properties"]["vault_id"].update(minimum=1),
])
def test_new_schema_features_fail_closed(mutate):
    schema = load_schema()
    mutate(schema)
    with pytest.raises(ContractError, match="UNSUPPORTED_SCHEMA"):
        check_schema(schema)


@pytest.mark.parametrize("value", [True, 1.0, "1", None, -1])
def test_integer_coordinates_are_strict(value):
    record = example("unit.json")
    record["locator"]["span"]["start"] = value
    with pytest.raises(ContractError, match="INVALID_SCHEMA"):
        validate_record("unit", record)


def test_no_runtime_dependencies_declared():
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []


def test_copied_module_runs_with_no_site_packages_or_pythonpath(tmp_path):
    # Simulate the future Skill layout, but do not modify the actual Skills.
    scripts = tmp_path / "isolated-skill/scripts"
    shutil.copytree(PACKAGE / "src/hermes_source_units", scripts / "lib/hermes_source_units",
                    ignore=shutil.ignore_patterns("__pycache__"))
    fixtures = tmp_path / "fixtures"
    shutil.copytree(PACKAGE / "examples", fixtures)
    entry = scripts / "validate.py"
    entry.write_text(
        "import sys, runpy, importlib.util\n"
        "from pathlib import Path\n"
        "assert sys.flags.isolated and sys.flags.no_site\n"
        "assert importlib.util.find_spec('jsonschema') is None\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))\n"
        "runpy.run_module('hermes_source_units', run_name='__main__')\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(PACKAGE / "src"))
    command = [sys.executable, "-I", "-S", str(entry), "knowledge_build",
               str(fixtures / "knowledge-build.json"), "--units", str(fixtures / "units.json")]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"]
    result = subprocess.run([*command[:4], "unit", str(fixtures / "invalid/missing-unit-id.json")],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["code"] == "INVALID_SCHEMA"
