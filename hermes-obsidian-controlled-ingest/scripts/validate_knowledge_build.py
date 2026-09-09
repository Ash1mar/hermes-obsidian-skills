#!/usr/bin/env python3
"""Read-only validation of knowledge-build decision records; not a factuality judge."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

CONTRACT = "hermes-knowledge-build/v3"
PREVIOUS_CONTRACT = "hermes-knowledge-build/v2"
LEGACY_CONTRACT = "hermes-knowledge-build/v1"
KINDS = {"entity", "concept", "requirement", "fact", "analysis"}
DECISIONS = {"create", "update", "reuse", "relate", "defer", "skip"}
OUTPUT_ROOTS = {"20_Notes", "30_Cards", "40_Concepts", "50_Projects", "_system"}
PROVENANCE_START = "<!-- knowledge-provenance:start -->"
PROVENANCE_END = "<!-- knowledge-provenance:end -->"


def authored_text(text: str) -> str:
    """Hash the reviewed page, excluding only the tool-owned provenance block."""
    if text.count(PROVENANCE_START) != text.count(PROVENANCE_END) or text.count(PROVENANCE_START) > 1:
        raise ValueError("malformed knowledge provenance block")
    if PROVENANCE_START in text:
        start, end = text.index(PROVENANCE_START), text.index(PROVENANCE_END)
        if end < start:
            raise ValueError("reversed knowledge provenance markers")
        text = text[:start] + text[end + len(PROVENANCE_END):]
    return text.replace("\r\n", "\n").strip() + "\n"


def authored_sha256(text: str) -> str:
    return hashlib.sha256(authored_text(text).encode("utf-8")).hexdigest()


def frontmatter_scalar(text: str, key: str) -> str:
    """Read a simple scalar; unsupported YAML shapes are rejected, never guessed."""
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        return ""
    values = re.findall(r"^" + re.escape(key) + r":\s*([^\n]+)", match[1], re.M)
    return values[0].strip().strip("\"'") if len(values) == 1 else ""


def output_provenance(data: dict) -> dict[str, list[dict]]:
    """Union all candidates supporting each output; navigation links are not evidence."""
    result: dict[str, list[dict]] = {}
    for candidate in data.get("candidates", []):
        if candidate.get("decision") in {"skip", "defer"}:
            continue
        for output in candidate.get("outputs", []):
            rows = result.setdefault(output, [])
            for evidence in candidate.get("evidence", []):
                row = {key: evidence[key] for key in ("path", "sha256", "lines", "qa", "qa_note") if key in evidence}
                supports = []
                for inspected in data.get("inspected_ranges", []):
                    if (inspected["path"] == evidence["path"] and inspected["sha256"] == evidence["sha256"]
                            and inspected["lines"][0] <= evidence["lines"][0] <= evidence["lines"][1] <= inspected["lines"][1]):
                        support = {k: inspected[k] for k in ("ledger_path", "bundle_id", "section_id") if k in inspected}
                        if support and support not in supports:
                            supports.append(support)
                row["sections"] = supports
                if row not in rows:
                    rows.append(row)
    return result


def render_provenance(rows: list[dict]) -> str:
    qa = "待核验草稿：以下疑点不阻断来源提取，也不代表已验证的工程结论。" if any(r["qa"] == "needs-qa" for r in rows) else "来源提取记录；不表示来源已获批准或工程结论已获专业验证。"
    return PROVENANCE_START + "\n## 来源与核验状态\n\n" + qa + "\n\n```json\n" + json.dumps(rows, ensure_ascii=False, indent=2) + "\n```\n" + PROVENANCE_END


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

    if not isinstance(data, dict) or data.get("contract") not in {CONTRACT, PREVIOUS_CONTRACT, LEGACY_CONTRACT}:
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
    current = data.get("contract") == CONTRACT
    if data.get("contract") in {CONTRACT, PREVIOUS_CONTRACT}:
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
            if current and item.get("qa") == "needs-qa" and not str(item.get("qa_note") or "").strip():
                errors.append(f"{label}: needs-qa requires a specific qa_note")
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
                    if phase != "complete" and section["status"] not in {"in_progress", "ingested", "skipped", "qa_required"}:
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
            if target and (phase != "plan" or decision in {"update", "reuse", "relate"}) and not target.is_file():
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
            if data.get("contract") in {CONTRACT, PREVIOUS_CONTRACT}:
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
                    if any(inspections[k].get("qa") != "usable" for k in matches) and not (current and item.get("qa") == "needs-qa"):
                        errors.append(f"{el}: inspection has unresolved QA")
                    for k in matches:
                        section = inspected_sections.get(k)
                        if section is not None and phase == "complete":
                            if section["status"] not in ({"ingested", "qa_required"} if current else {"ingested"}):
                                errors.append(f"{el}: productive evidence section must be ingested")
                            if current and item.get("qa") == "needs-qa" and item.get("qa_note") not in section.get("qa_items", []):
                                errors.append(f"{el}: qa_note missing from supporting section ledger")
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
            draft_qa = current and item.get("qa") == "needs-qa" and isinstance(item.get("qa_note"), str) and bool(item["qa_note"].strip())
            if decision not in {"skip", "defer"} and item.get("qa") != "usable" and not draft_qa:
                errors.append(f"{el}: QA-restricted evidence cannot support a knowledge output")
    if current and not errors:
        reviews = data.get("output_reviews", [])
        if not isinstance(reviews, list) or any(not isinstance(r, dict) for r in reviews):
            return errors + ["output_reviews: expected array of page reviews"]
        reviewed = {r.get("path"): r for r in reviews if isinstance(r.get("path"), str)}
        if len(reviewed) != len(reviews):
            errors.append("output_reviews: invalid or duplicate output path")
        for output, rows in output_provenance(data).items():
            target = path(output, "output", output=True)
            if not target or not target.is_file():
                continue  # Missing outputs were handled above (plan permits create).
            text = target.read_text(encoding="utf-8")
            if any(r["qa"] == "needs-qa" for r in rows) and frontmatter_scalar(text, "status") != "draft":
                errors.append(f"{output}: QA knowledge output must have status: draft")
            if phase in {"written", "complete"}:
                review = reviewed.get(output, {})
                if review.get("authored_sha256") != authored_sha256(text) or not str(review.get("finding") or "").strip():
                    errors.append(f"{output}: final model review missing or page changed after review")
                if output.startswith("50_Projects/") and not str(review.get("project_basis") or "").strip():
                    errors.append(f"{output}: specific project basis required; use a theme index otherwise")
                if frontmatter_scalar(text, "evidence_mode") == "index":
                    scope = "multi-source" if len({r["path"] for r in rows}) > 1 else "single-source"
                    if (frontmatter_scalar(text, "evidence_scope") != scope
                            or frontmatter_scalar(text, "evidence_coverage") not in {"complete", "representative"}
                            or frontmatter_scalar(text, "evidence_authority") != "navigation"):
                        errors.append(f"{output}: index requires matching scope, coverage and navigation authority")
                if phase == "complete" and render_provenance(rows) not in text:
                    errors.append(f"{output}: generated provenance differs from all supporting candidates")
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
