from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "hermes-obsidian-vault-bootstrap" / "scripts" / "init_obsidian_vault.py"


def test_bootstrap_keeps_runtime_skills_outside_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--vault-path",
            str(vault),
            "--profile",
            "general",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (vault / "_system" / "skills").exists()
    assert (vault / "_system" / "templates" / "ingest-log-template.md").is_file()
    report = next((vault / "_system" / "reports").glob("vault-setup-*.md"))
    assert "copied skill note" not in report.read_text(encoding="utf-8")
