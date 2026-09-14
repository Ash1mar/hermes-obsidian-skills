"""P1 deployment and bootstrap acceptance, using only copied Skill payloads."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(script, *args):
    return subprocess.run([sys.executable, "-I", "-S", str(script), *map(str, args)],
                          cwd=script.parent, capture_output=True, text=True)


@pytest.fixture
def copied(tmp_path):
    target = tmp_path / "standalone-skill"
    shutil.copytree(ROOT / "hermes-obsidian-vault-bootstrap", target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return target


@pytest.mark.parametrize("profile", ["general", "engineering", "meeting"])
def test_isolated_bootstrap_and_no_overwrite(copied, tmp_path, profile):
    vault = tmp_path / "new-vault"
    script = copied / "scripts/init_obsidian_vault.py"
    result = run(script, "--vault-path", vault, "--profile", profile)
    assert result.returncode == 0, result.stderr
    check = run(copied / "scripts/validate_source_unit_vault.py", "--vault", vault)
    assert check.returncode == 0, check.stdout + check.stderr
    data = json.loads(check.stdout)
    assert data["bootstrap_ready"] and not data["query_ready"]
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    for extra in [[], ["--force-empty"]]:
        assert run(script, "--vault-path", vault, *extra).returncode != 0
        assert before == {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def test_config_override_validation_and_tamper(copied, tmp_path):
    config = json.loads((copied / "lib/hermes_source_units/defaults/config.json").read_text(encoding="utf-8"))
    override = tmp_path / "custom.json"
    vault = tmp_path / "vault"
    script = copied / "scripts/init_obsidian_vault.py"
    config["reading"]["max_codepoints"] = 20000
    override.write_text(json.dumps(config))
    assert run(script, "--vault-path", vault, "--source-unit-config", override).returncode == 0
    check_script = copied / "scripts/validate_source_unit_vault.py"
    check = json.loads(run(check_script, "--vault", vault).stdout)
    assert check["effective_config"]["reading"]["max_codepoints"] == 20000
    config["reading"]["max_codepoints"] = 21000
    (vault / "_system/metadata/source-unit-config.json").write_text(json.dumps(config))
    assert run(check_script, "--vault", vault).returncode == 1
    config["unknown"] = True
    override.write_text(json.dumps(config))
    invalid_target = tmp_path / "invalid"
    assert run(script, "--vault-path", invalid_target, "--source-unit-config", override).returncode != 0
    assert not invalid_target.exists()


def test_missing_directory_and_false_ready(copied, tmp_path):
    vault = tmp_path / "vault"
    assert run(copied / "scripts/init_obsidian_vault.py", "--vault-path", vault).returncode == 0
    check = copied / "scripts/validate_source_unit_vault.py"
    directory = vault / "_system/sources/units"
    directory.rmdir()
    assert run(check, "--vault", vault).returncode == 1
    directory.mkdir()
    manifest = vault / "_system/vault.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["source_units"]["capabilities"]["retrieval"] = True
    manifest.write_text(json.dumps(value))
    assert run(check, "--vault", vault).returncode == 1


def test_embedded_runtime_matches_canonical():
    result = run(ROOT / "hermes-source-units/tools/sync_skill_runtime.py", "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_copied_lint_does_not_claim_query_ready(copied, tmp_path):
    vault = tmp_path / "vault"
    assert run(copied / "scripts/init_obsidian_vault.py", "--vault-path", vault).returncode == 0
    lint = tmp_path / "lint"
    shutil.copytree(ROOT / "hermes-obsidian-vault-lint", lint)
    result = run(lint / "scripts/lint_vault.py", "--vault", vault, "--profile", "query-ready", "--json")
    data = json.loads(result.stdout)
    assert not data["ok"]
    assert any(i["code"] == "source_units.pipeline_pending" for i in data["issues"])
