"""P2 source-plane acceptance using the copied ingest Skill runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap/scripts/init_obsidian_vault.py"
LINT = ROOT / "hermes-obsidian-vault-lint/scripts/lint_vault.py"
INGEST = ROOT / "hermes-obsidian-controlled-ingest"


def execute(script: Path, *arguments: object, expected: int = 0):
    result = subprocess.run([sys.executable, "-I", "-S", str(script), *map(str, arguments)],
                            cwd=script.parent, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == expected, result.stdout + result.stderr
    stream = result.stdout if expected == 0 else result.stderr
    return json.loads(stream) if stream.strip().startswith("{") else result


def register(vault: Path, source_sha: str, *, active: bool = True) -> None:
    registry_path = vault / "_system/metadata/document-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["registry_revision"] = 1
    registry["records"] = [{
        "document_id": "doc-demo", "version_id": "version-demo-1",
        "resource_id": "resource-demo-1", "content_sha256": source_sha,
        "processing_status": "completed", "governance_status": "active" if active else "candidate",
        "source_occurrences": [{"source_organization_id": "organization-demo"}],
    }]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    organizations_path = vault / "_system/metadata/source-organizations.json"
    organizations = json.loads(organizations_path.read_text(encoding="utf-8"))
    organizations["registry_revision"] = 1
    organizations["organizations"] = [{"id": "organization-demo", "status": "approved"}]
    organizations_path.write_text(json.dumps(organizations), encoding="utf-8")


def lint_report(vault: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(LINT), "--vault", str(vault),
         "--profile", "post-ingest", "--ingest-skill-path", str(INGEST), "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def runtime(tmp_path: Path):
    ingest = tmp_path / "isolated-ingest"
    shutil.copytree(ROOT / "hermes-obsidian-controlled-ingest", ingest,
                    ignore=shutil.ignore_patterns("__pycache__"))
    config = json.loads((ingest / "lib/hermes_source_units/defaults/config.json").read_text(encoding="utf-8"))
    config["source"].update({"target_codepoints": 45, "max_codepoints": 70,
                             "overlap_codepoints": 10})
    config_path = tmp_path / "source-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    vault = tmp_path / "vault"
    result = subprocess.run([sys.executable, "-I", "-S", str(BOOTSTRAP), "--vault-path", str(vault),
                             "--source-unit-config", str(config_path)], capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return vault, ingest / "scripts/manage_source_units.py"


def prepare_markdown(runtime, text: str):
    vault, script = runtime
    raw = text.replace("\n", "\r\n").encode("utf-8")
    source = vault / "10_Raw/demo.md"
    source.write_bytes(raw)
    register(vault, hashlib.sha256(raw).hexdigest())
    prepared = execute(script, "--vault", vault, "prepare-markdown", "--source", "10_Raw/demo.md",
                       "--document-id", "doc-demo", "--version-id", "version-demo-1",
                       "--resource-id", "resource-demo-1")
    return prepared


def test_markdown_build_is_deterministic_exact_and_self_contained(runtime, tmp_path):
    vault, script = runtime
    text = ("preamble 🧪\n\n# First\nAlpha sentence. Beta sentence. Gamma sentence.\n\n"
            "```python\n" + "x = 'protected structure'\n" * 4 + "```\n\n"
            "## Second\n| key | value |\n| --- | --- |\n| name | pump |\n")
    prepared = prepare_markdown(runtime, text)
    artifact = prepared["artifact_manifest"]
    preview = execute(script, "--vault", vault, "preview", "--artifact-manifest", artifact,
                      "--actor", "tester", "--expected-revision", 0)
    assert preview["mode"] == "preview"
    assert not (vault / "_system/sources/units/resource-demo-1/current.json").exists()
    # Simulate a crash after the immutable directory move but before current.json.
    orphan = vault / "_system/sources/units/resource-demo-1" / preview["manifest"]["unit_set_id"]
    orphan.mkdir(parents=True)
    for name, value in (("manifest.json", preview["manifest"]),
                        ("sections.json", preview["sections"]),
                        ("diagnostics.json", preview["diagnostics"])):
        (orphan / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    (orphan / "units.jsonl").write_text(
        "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in preview["units"]), encoding="utf-8")
    built = execute(script, "--vault", vault, "build", "--artifact-manifest", artifact,
                    "--actor", "tester", "--expected-revision", 0)
    assert built["mode"] == "published"
    assert built["manifest"]["unit_set_id"] == preview["manifest"]["unit_set_id"]
    assert built["manifest"]["unit_ids"] == preview["manifest"]["unit_ids"]
    assert any(item["code"] == "oversized-protected-structure" for item in built["diagnostics"])
    validated = execute(script, "--vault", vault, "validate", "--resource-id", "resource-demo-1")
    assert validated["unit_count"] == len(built["units"])
    repeated = execute(script, "--vault", vault, "build", "--artifact-manifest", artifact,
                       "--actor", "tester", "--expected-revision", 1)
    assert repeated["manifest"]["revision"] == 1
    units = execute(script, "--vault", vault, "list", "--resource-id", "resource-demo-1")["units"]
    normalized = (vault / Path(artifact).parent / "document.md").read_text(encoding="utf-8")
    spans = [unit["locator"]["span"] for unit in units if unit["locator"]["kind"] == "text"]
    cursor = 0
    for span in spans:
        assert span["start"] <= cursor < span["end"]
        cursor = max(cursor, span["end"])
    assert cursor == len(normalized)
    assert all(left["end"] >= right["start"] for left, right in zip(spans, spans[1:]))
    text_units = [unit for unit in units if unit["locator"]["kind"] == "text"]
    assert all(left["section_id"] == right["section_id"]
               for left, right in zip(text_units, text_units[1:])
               if left["locator"]["span"]["end"] > right["locator"]["span"]["start"])

    source_ref = {"unit_ref": units[0]["ref"], "span": None}
    ref_path = tmp_path / "source-ref.json"
    ref_path.write_text(json.dumps(source_ref), encoding="utf-8")
    result = execute(script, "--vault", vault, "get", "--source-ref", ref_path,
                     "--actor", "tester", "--purpose", "construction", "--registry-revision", 1)
    locator = units[0]["locator"]["span"]
    assert result["core_text"] == normalized[locator["start"]:locator["end"]]
    wrong = execute(script, "--vault", vault, "get", "--source-ref", ref_path,
                    "--actor", "tester", "--purpose", "construction", "--registry-revision", 0,
                    expected=2)
    assert wrong["code"] == "REVISION_CONFLICT"


def test_context_budget_and_tamper_detection(runtime, tmp_path):
    vault, script = runtime
    prepared = prepare_markdown(runtime, "# One\n" + "sentence. " * 30 + "\n")
    built = execute(script, "--vault", vault, "build", "--artifact-manifest", prepared["artifact_manifest"],
                    "--actor", "tester", "--expected-revision", 0)
    units = [item for item in built["units"] if item["locator"]["kind"] == "text"]
    spans = [item["locator"]["span"] for item in units]
    assert any(left["end"] > right["start"] for left, right in zip(spans, spans[1:]))
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps([{"unit_ref": units[0]["ref"], "span": None}]), encoding="utf-8")
    context = execute(script, "--vault", vault, "context", "--core-refs", refs,
                      "--actor", "tester", "--purpose", "construction", "--registry-revision", 1,
                      "--max-codepoints", len(context_text(vault, prepared, units[0])) + 1)
    assert context["truncated"] and context["omitted_refs"]
    healthy = lint_report(vault)
    assert healthy["metrics"]["source_unit_count"] == len(built["units"])
    assert not any(issue["code"] == "source_units.invalid_repository" for issue in healthy["issues"])
    document = vault / Path(prepared["artifact_manifest"]).parent / "document.md"
    document.write_text(document.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
    failure = execute(script, "--vault", vault, "validate", "--resource-id", "resource-demo-1", expected=2)
    assert failure["code"] == "SOURCE_CHANGED"
    damaged = lint_report(vault)
    assert any(issue["code"] == "source_units.invalid_repository" for issue in damaged["issues"])


def context_text(vault: Path, prepared: dict, unit: dict) -> str:
    text = (vault / Path(prepared["artifact_manifest"]).parent / "document.md").read_text(encoding="utf-8")
    span = unit["locator"]["span"]
    return text[span["start"]:span["end"]]


def test_bundle_outline_asset_and_revision_guard(runtime):
    vault, script = runtime
    bundle = vault / "10_Raw/converted/demo_bundle"
    (bundle / "tables").mkdir(parents=True)
    document = "<!-- source-page: 1 -->\n# Parent\nintro\n## Child\nchild\n"
    (bundle / "document.md").write_text(document, encoding="utf-8")
    table = b"| A | B |\n|---|---|\n|1|2|\n"
    (bundle / "tables/data.md").write_bytes(table)
    outline = {"schema_version": "2.0", "sections": [
        {"id": "parent", "title": "Parent", "level": 1, "parent": None, "path": ["Parent"],
         "start_line": 2, "end_line": 5, "pages": [1], "assets": [], "quality": "pass"},
        {"id": "child", "title": "Child", "level": 2, "parent": "parent", "path": ["Parent", "Child"],
         "start_line": 4, "end_line": 5, "pages": [1], "assets": ["table-1"], "quality": "needs-review"},
    ]}
    (bundle / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    source_sha = "a" * 64
    manifest = {"schema_version": "2.0", "source": {"sha256": source_sha},
                "document": {"path": "document.md"}, "outline": {"path": "outline.json"},
                "images": [], "tables": [{"id": "table-1", "path": "tables/data.md", "sha256": hashlib.sha256(table).hexdigest(), "pages": [1]}],
                "governance": {"vault_id": json.loads((vault / "_system/vault.json").read_text())["vault"]["id"],
                               "document_id": "doc-demo", "version_id": "version-demo-1", "resource_id": "resource-demo-1"}}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    register(vault, source_sha)
    prepared = execute(script, "--vault", vault, "prepare-bundle", "--bundle", "10_Raw/converted/demo_bundle")
    built = execute(script, "--vault", vault, "build", "--artifact-manifest", prepared["artifact_manifest"],
                    "--actor", "tester", "--expected-revision", 0)
    assert any(unit["content_type"] == "source_asset" for unit in built["units"])
    assert any(unit["quality_refs"] for unit in built["units"] if unit["section_id"] == "child")
    parent = next(section for section in built["sections"] if section["section_id"] == "parent")
    child = next(section for section in built["sections"] if section["section_id"] == "child")
    assert parent["owned_ranges"][0]["end"] == child["scope"]["start"]
    child_unit = next(unit for unit in built["units"]
                      if unit["section_id"] == "child" and unit["locator"]["kind"] == "text")
    refs = vault / "child-refs.json"
    refs.write_text(json.dumps([{"unit_ref": child_unit["ref"], "span": None}]), encoding="utf-8")
    context = execute(script, "--vault", vault, "context", "--core-refs", refs,
                      "--actor", "tester", "--purpose", "construction", "--registry-revision", 1,
                      "--max-codepoints", 1000)
    assert "parent" in {item["section_id"] for item in context["context"]}
    conflict = execute(script, "--vault", vault, "build", "--artifact-manifest", prepared["artifact_manifest"],
                       "--actor", "tester", "--expected-revision", 0, expected=2)
    assert conflict["code"] == "REVISION_CONFLICT"


def test_auto_uses_numbered_heading_heuristic(runtime):
    vault, script = runtime
    prepared = prepare_markdown(runtime, "前言。\n\n一、适用范围\n范围正文。\n\n二、例外\n例外正文。\n")
    preview = execute(script, "--vault", vault, "preview",
                      "--artifact-manifest", prepared["artifact_manifest"],
                      "--actor", "tester", "--expected-revision", 0)
    route = next(item for item in preview["diagnostics"] if item["code"] == "strategy-selected")
    assert route["selected"] == "heuristic"
    assert {section["title"] for section in preview["sections"]} >= {"一、适用范围", "二、例外"}
