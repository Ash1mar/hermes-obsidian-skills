#!/usr/bin/env python3
"""Project reviewed build evidence into pages and existing section ledgers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from validate_knowledge_build import (
    CONTRACT, PROVENANCE_START, PROVENANCE_END, authored_sha256,
    output_provenance, render_provenance, validate_record,
)


def synchronize(record: Path, vault: Path, apply: bool = False) -> dict:
    vault = vault.resolve()
    data = json.loads(record.read_text(encoding="utf-8"))
    if data.get("contract") != CONTRACT:
        raise ValueError("provenance synchronization requires the current build contract")
    errors = validate_record(data, vault, "written" if apply else "prepare")
    if errors:
        raise ValueError("; ".join(errors))
    outputs = output_provenance(data)
    pages = []
    registrations = {}
    for output, rows in outputs.items():
        target = vault / output
        original = target.read_text(encoding="utf-8")
        block = render_provenance(rows)
        if PROVENANCE_START in original:
            begin, end = original.index(PROVENANCE_START), original.index(PROVENANCE_END) + len(PROVENANCE_END)
            updated = original[:begin] + block + original[end:]
        else:
            updated = original.rstrip() + "\n\n" + block + "\n"
        pages.append((target, original, updated))
        for row in rows:
            for support in row["sections"]:
                key = (support["ledger_path"], support["section_id"])
                registration = registrations.setdefault(key, {"outputs": set(), "qa_items": set()})
                registration["outputs"].add(output)
                if row["qa"] == "needs-qa":
                    registration["qa_items"].add(row["qa_note"])

    # Check all model-provided paths before any write; source-map paths may be host absolute.
    for ledger_name, section_id in registrations:
        ledger = json.loads((vault / ledger_name).read_text(encoding="utf-8"))
        source_map = Path(ledger.get("control", {}).get("source_map_path") or (vault / ledger_name).with_suffix(".source-map.md"))
        if not source_map.is_absolute() or not source_map.resolve().is_relative_to(vault / "_system" / "reports"):
            raise ValueError(f"unsafe or foreign-host source map: {source_map}; reconcile ledger in this runtime first")
    result = {"mode": "apply" if apply else "preview", "outputs": list(outputs),
              "review_inputs": [{"path": target.relative_to(vault).as_posix(), "authored_sha256": authored_sha256(original)}
                                for target, original, _ in pages],
              "supporting_sections": len(registrations)}
    if not apply:
        return result
    # Do not change section completion states: the agent decides reading coverage and QA outcome.
    for (ledger_name, section_id), registration in sorted(registrations.items()):
        ledger_path = vault / ledger_name
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        section = next(s for s in ledger["sections"] if s["id"] == section_id)
        add_outputs = sorted(registration["outputs"] - set(section.get("outputs", [])))
        add_qa = sorted(registration["qa_items"] - set(section.get("qa_items", [])))
        if not add_outputs and not add_qa:
            continue
        command = [sys.executable, str(Path(__file__).with_name("manage_bundle_ingest.py")), "update", str(ledger_path),
                   "--section", section_id, "--expected-revision", str(ledger["revision"]), "--json"]
        for output in add_outputs:
            command.extend(["--output", output])
        for note in add_qa:
            command.extend(["--qa-item", note])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        if completed.returncode:
            raise ValueError(f"partial synchronization; safely rerun after resolving: {completed.stderr}")
    for target, original, updated in pages:
        if target.read_text(encoding="utf-8") != original:
            raise ValueError(f"page changed during synchronization: {target}")
        if updated == original:
            continue
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent, delete=False) as stream:
            stream.write(updated)
            temporary = Path(stream.name)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Apply reviewed provenance; default is read-only preview")
    args = parser.parse_args()
    try:
        result = synchronize(args.record, args.vault, args.apply)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
