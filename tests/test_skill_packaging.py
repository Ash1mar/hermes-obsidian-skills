from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skill_entry_points_are_tracked_as_executable() -> None:
    scripts = sorted(ROOT.glob("hermes-obsidian-*/scripts/*.py"))
    assert scripts

    completed = subprocess.run(
        ["git", "ls-files", "--stage", "--", "hermes-obsidian-*/scripts/*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_modes = {
        line.split(maxsplit=3)[3].replace("\\", "/"): line.split(maxsplit=1)[0]
        for line in completed.stdout.splitlines()
        if line.strip()
    }

    expected = [script.relative_to(ROOT).as_posix() for script in scripts]
    assert sorted(tracked_modes) == expected
    assert all(tracked_modes[path] == "100755" for path in expected)
    assert all(script.read_text(encoding="utf-8").startswith("#!") for script in scripts)
