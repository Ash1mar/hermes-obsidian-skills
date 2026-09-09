#!/usr/bin/env python3
"""Read-only validation of knowledge-build decision records; not a factuality judge."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

CONTRACT = "hermes-knowledge-build/v2"
LEGACY_CONTRACT = "hermes-knowledge-build/v1"
KINDS = {"entity", "concept", "requirement", "fact", "analysis"}
DECISIONS = {"create", "update", "reuse", "relate", "defer", "skip"}
OUTPUT_ROOTS = {"20_Notes", "30_Cards", "40_Concepts", "50_Projects", "_system"}


def validate_record(data: object, vault: Path, phase: str = "complete") -> list[str]:
    """Validate paths, evidence fingerprints/ranges and decision consistency only."""
    errors: list[str] = []
    vault = vault.resolve()

    def path(value: object, label: str, output: bool = False) -> Path | None:
        if not isinstance(value, str) or not value or "\\" in value:
            errors.append(f"{label}: expected Vault-relative POSIX path")
            return None
        rel = PurePosixPath(value)
        if rel.is_absolute() or ".." in rel.parts or ":" in value:
            errors.append(f"{label}: unsafe path")
            return None
        resolved = (vault / value).resolve()
        if not resolved.is_relative_to(vault):
            errors.append(f"{label}: path escapes Vault")
            return None
        if output and (not rel.parts or rel.parts[0] not in OUTPUT_ROOTS
                       or rel.suffix.lower() != ".md"
                       or (rel.parts[0] == "_system" and rel.parts[:2] != ("_system", "reports"))):
            errors.append(f"{label}: not a derived knowledge Markdown path")
            return None
        return resolved

    if not isinstance(data, dict) or data.get("contract") not in {CONTRACT, LEGACY_CONTRACT}:
        return ["invalid knowledge-build contract"]
    if not isinstance(data.get("scope"), str) or not data["scope"].strip():
        errors.append("scope: describe the inspected source scope")
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return errors + ["candidates: expected array"]
    if not candidates and not data.get("empty_reason"):
        errors.append("empty_reason: required when no candidates were found")
    inspections = []
    inspected_sections = {}
    if data.get("contract") == CONTRACT:
        status = data.get("execution_status")
        if status not in {"in_progress", "completed", "blocked", "not_started"}:
            errors.append("execution_status: invalid or missing")
        if phase == "complete" and status != "completed":
            errors.append("execution_status: knowledge unit is not completed")
        inspections = data.get("inspected_ranges")
        if not isinstance(inspections, list) or not inspections:
            errors.append("inspected_ranges: actual source inspection is required")
            inspections = []
        for i, item in enumerate(inspections):
            label = f"inspected_ranges[{i}]"
            if not isinstance(item, dict):
                errors.append(f"{label}: expected object")
                continue
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                errors.append(f"{label}: inspection finding/reason required")
            if item.get("qa") not in {"usable", "needs-qa"}:
                errors.append(f"{label}: invalid QA classification")
            # Reuse the source/path/hash/range checks even for zero candidates.
            probe = {"contract": LEGACY_CONTRACT, "scope": label, "candidates": [{
                "id": "inspection", "name": label, "kind": "fact",
                "identity_rationale": "Inspection only", "reason": "Inspection only",
                "decision": "skip", "outputs": [], "evidence": [item],
            }]}
            errors.extend(f"{label}: {error}" for error in validate_record(probe, vault, phase))
            source = path(item.get("path"), label)
            if source and (source.parent / "manifest.json").is_file():
                ledger_path = path(item.get("ledger_path"), label + ".ledger_path")
                try:
                    if ledger_path is None:
                        continue
                    if not ledger_path.relative_to(vault).as_posix().startswith("_system/reports/"):
                        raise ValueError("ledger must be under _system/reports")
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    manifest = json.loads((source.parent / "manifest.json").read_text(encoding="utf-8"))
                    bundle_id = "bundle-v2-" + manifest["source"]["sha256"][:16]
                    if item.get("bundle_id") != ledger["bundle_id"] or ledger["bundle_id"] != bundle_id:
                        raise ValueError("Bundle identity mismatch")
                    if ledger["bundle"]["document_sha256"] != item.get("sha256"):
                        raise ValueError("ledger document fingerprint mismatch")
                    revision = item.get("ledger_revision")
                    if type(revision) is not int or not 1 <= revision <= ledger["revision"]:
                        raise ValueError("invalid observed ledger revision")
                    section = next(s for s in ledger["sections"] if s["id"] == item.get("section_id"))
                    lo, hi = item["lines"]
                    if not any(r["start_line"] <= lo <= hi <= r["end_line"] for r in section["content_ranges"]):
                        raise ValueError("inspection is outside section content_ranges")
                    if phase == "complete" and section["status"] not in {"ingested", "skipped", "qa_required"}:
                        raise ValueError("inspected section is not terminal")
                    if phase == "plan" and section["status"] not in {"in_progress", "ingested", "skipped", "qa_required"}:
                        raise ValueError("section must be claimed before planning knowledge writes")
                    inspected_sections[i] = section
                except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
                    errors.append(f"{label}: invalid ledger inspection ({exc})")
    seen = set()
    for i, candidate in enumerate(candidates):
        label = f"candidates[{i}]"
        if not isinstance(candidate, dict):
            errors.append(f"{label}: expected object")
            continue
        cid = candidate.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", cid):
            errors.append(f"{label}: invalid local candidate id")
        elif cid in seen:
            errors.append(f"{label}: duplicate candidate id")
        else:
            seen.add(cid)
        for field in ("name", "identity_rationale", "reason"):
            if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                errors.append(f"{label}.{field}: nonempty text required")
        kind, decision = candidate.get("kind"), candidate.get("decision")
        if kind not in KINDS or decision not in DECISIONS:
            errors.append(f"{label}: invalid kind or decision")
        outputs = candidate.get("outputs", [])
        if not isinstance(outputs, list):
            errors.append(f"{label}.outputs: expected array")
            outputs = []
        if decision in {"create", "update", "reuse", "relate"} and not outputs:
            errors.append(f"{label}: productive decision requires outputs")
        if decision in {"defer", "skip"} and outputs:
            errors.append(f"{label}: deferred/skipped candidates cannot claim knowledge outputs")
        for output in outputs:
            target = path(output, label + ".outputs", output=True)
            if target and phase == "plan" and decision == "create" and target.exists():
                errors.append(f"{label}: create target already exists; inspect and choose update/reuse")
            if target and (phase == "complete" or decision in {"update", "reuse", "relate"}) and not target.is_file():
                errors.append(f"{label}: missing output {output}")
        inspected = candidate.get("existing_targets", [])
        if not isinstance(inspected, list):
            errors.append(f"{label}.existing_targets: expected array")
            inspected = []
        for existing in inspected:
            target = path(existing, label + ".existing_targets", output=True)
            if target and not target.is_file():
                errors.append(f"{label}: missing inspected target {existing}")
        if decision in {"update", "reuse", "relate"} and not inspected:
            errors.append(f"{label}: existing target inspection required")
        if decision in {"update", "reuse"} and any(o not in inspected for o in outputs):
            errors.append(f"{label}: update/reuse outputs must have been inspected")
        if kind == "analysis" and decision not in {"skip", "defer"} and not candidate.get("derivation"):
            errors.append(f"{label}: analysis requires a derivation distinct from source facts")
        evidence = candidate.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"{label}.evidence: expected array")
            evidence = []
        if decision not in {"skip", "defer"} and not evidence:
            errors.append(f"{label}: supporting evidence required")
        for j, item in enumerate(evidence):
            el = f"{label}.evidence[{j}]"
            if not isinstance(item, dict):
                errors.append(f"{el}: expected object")
                continue
            if data.get("contract") == CONTRACT:
                matches = []
                for k, inspected in enumerate(inspections):
                    try:
                        if (inspected["path"] == item["path"] and inspected["sha256"] == item["sha256"]
                                and inspected["lines"][0] <= item["lines"][0] <= item["lines"][1] <= inspected["lines"][1]):
                            matches.append(k)
                    except (KeyError, TypeError, IndexError):
                        continue
                if not matches:
                    errors.append(f"{el}: evidence is outside inspected_ranges")
                if decision not in {"skip", "defer"}:
                    if any(inspections[k].get("qa") != "usable" for k in matches):
                        errors.append(f"{el}: inspection has unresolved QA")
                    for k in matches:
                        section = inspected_sections.get(k)
                        if section is not None and phase == "complete":
                            if section["status"] != "ingested":
                                errors.append(f"{el}: productive evidence section must be ingested")
                            if any(o not in section.get("outputs", []) for o in outputs):
                                errors.append(f"{el}: output missing from supporting section ledger")
            source = path(item.get("path"), el)
            if not source or not source.is_file():
                errors.append(f"{el}: source missing")
                continue
            if not source.relative_to(vault).parts[0] == "10_Raw":
                errors.append(f"{el}: evidence must reference source/normalized text under 10_Raw")
            if hashlib.sha256(source.read_bytes()).hexdigest() != item.get("sha256"):
                errors.append(f"{el}: source fingerprint changed")
            lines = item.get("lines")
            if not isinstance(lines, list) or len(lines) != 2 or any(type(n) is not int for n in lines) or not 1 <= lines[0] <= lines[1]:
                errors.append(f"{el}: expected inclusive 1-based line range")
            else:
                try:
                    if lines[1] > len(source.read_text(encoding="utf-8").splitlines()):
                        errors.append(f"{el}: range exceeds source")
                except (UnicodeError, OSError):
                    errors.append(f"{el}: use normalized UTF-8 text, not a binary original")
            if decision not in {"skip", "defer"} and item.get("qa") != "usable":
                errors.append(f"{el}: QA-restricted evidence cannot support a knowledge output")
    return errors


def validate_file(record: Path, vault: Path, phase: str = "complete") -> list[str]:
    try:
        return validate_record(json.loads(record.read_text(encoding="utf-8")), vault, phase)
    except (OSError, ValueError, TypeError) as exc:
        return [f"unreadable/invalid build record: {exc}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--phase", choices=["plan", "complete"], default="complete")
    parser.add_argument("--require-current", action="store_true", help="Reject legacy records for newly performed work")
    args = parser.parse_args()
    errors = validate_file(args.record, args.vault, args.phase)
    if args.require_current and not errors:
        if json.loads(args.record.read_text(encoding="utf-8")).get("contract") != CONTRACT:
            errors.append("new knowledge construction requires the current contract")
    print(json.dumps({"ok": not errors, "issues": errors, "phase": args.phase}, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
