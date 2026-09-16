#!/usr/bin/env python3
"""Generate/check self-contained Skill copies of the canonical stdlib runtime."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "hermes-source-units/src/hermes_source_units"
TARGETS = ("hermes-obsidian-vault-bootstrap", "hermes-obsidian-vault-lint",
           "hermes-obsidian-controlled-ingest", "hermes-obsidian-knowledge-finalize")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = {p.relative_to(SOURCE): p.read_text(encoding="utf-8").encode("utf-8") for p in SOURCE.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    for skill in TARGETS:
        target = ROOT / skill / "lib/hermes_source_units"
        expected = dict(files)
        expected[Path("embedded-manifest.json")] = (json.dumps(
            {str(p).replace("\\", "/"): hashlib.sha256(b).hexdigest()
             for p, b in sorted(files.items())}, indent=2) + "\n").encode()
        if args.check:
            actual = {p.relative_to(target) for p in target.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
            if actual != set(expected) or any((target / p).read_text(encoding="utf-8").encode("utf-8") != b for p, b in expected.items()):
                raise SystemExit(f"Embedded runtime drift: {skill}")
        else:
            for p, b in expected.items():
                (target / p).parent.mkdir(parents=True, exist_ok=True)
                (target / p).write_bytes(b)
    print("Embedded Skill runtimes verified" if args.check else "Embedded Skill runtimes generated")


if __name__ == "__main__":
    main()
