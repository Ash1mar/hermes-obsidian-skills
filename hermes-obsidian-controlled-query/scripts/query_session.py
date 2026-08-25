#!/usr/bin/env python3
"""Run the low-round-trip begin, inspect, and finalize controlled-query workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from manage_query_trace import (
    clean_event,
    finalize_trace,
    grouped_request_metrics,
    grouped_states,
    load_state,
    now_iso,
    resolve_claim_text,
    start_trace,
    write_state,
)
from retrieve_query_scope import compact_result, load_projections, retrieve_scope


WORKFLOW = "query-session/v2"
QUESTION_MARK_RE = re.compile(r"[?？]")
NUMBERED_QUESTION_RE = re.compile(
    r"(?m)^\s*(?:\d{1,2}[.)、．]|[（(]\d{1,2}[)）]|[一二三四五六七八九十]{1,3}[、.．])\s*\S+"
)
DECISION_KEYS = {
    "status",
    "evidence_level",
    "claims",
    "verified_evidence_refs",
    "events",
    "conclusion",
    "unresolved",
    "unresolved_items",
}
CLAIM_KEYS = {"text", "claim", "statement", "claim_text", "status", "evidence_refs", "qualification"}
EVENT_KEYS = {
    "stage",
    "route",
    "status",
    "summary",
    "evidence_refs",
    "inspected_paths",
    "hit_count",
    "duration_ms",
    "accounting",
    "extensions",
}
VISUAL_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_INSPECTIONS = 2
MAX_SUPPLEMENTS = 1


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def vault_relative(path: Path, vault_root: Path) -> str:
    return path.resolve().relative_to(vault_root.resolve()).as_posix()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@lru_cache(maxsize=128)
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def question_shape(question: str) -> dict[str, int | bool]:
    question_marks = len(QUESTION_MARK_RE.findall(question))
    numbered_items = len(NUMBERED_QUESTION_RE.findall(question))
    detected_count = max(question_marks, numbered_items)
    return {
        "question_marks": question_marks,
        "numbered_items": numbered_items,
        "detected_count": detected_count,
        "multiple_detected": detected_count >= 2,
    }


def validate_question_boundary(args: argparse.Namespace) -> dict[str, int | bool]:
    shape = question_shape(args.question)
    reason = str(args.coupled_reason or "").strip()
    if args.coupled and not reason:
        raise ValueError("--coupled requires a non-empty --coupled-reason")
    if reason and not args.coupled:
        raise ValueError("--coupled-reason requires --coupled")
    if shape["multiple_detected"] and not args.coupled:
        raise ValueError(
            "multiple questions detected before trace creation; use one shared --request-id and call begin "
            "once per question with --question-index, or use --coupled with --coupled-reason only when all "
            "subparts require the same evidence set"
        )
    return shape


def validate_request_sequence(
    vault_root: Path,
    request_id: str | None,
    question_index: int | None,
    question_count: int | None,
) -> None:
    if question_count is not None and question_count < 1:
        raise ValueError("question count must be at least 1")
    if question_count is not None and not request_id:
        raise ValueError("question count requires request id")
    if question_count is not None and question_index is None:
        raise ValueError("question count requires question index")
    if question_count is not None and question_index is not None and question_index > question_count:
        raise ValueError("question index must not exceed question count")
    if not request_id:
        return
    states = grouped_states(vault_root, request_id)
    open_traces = [str(state.get("trace_id")) for state in states if state.get("status") == "in_progress"]
    if open_traces:
        raise ValueError(
            f"request already has an in-progress trace; finalize it before begin: {', '.join(open_traces)}"
        )
    indexed = {int(state["question_index"]): state for state in states if state.get("question_index") is not None}
    if question_index is not None:
        if question_index in indexed:
            raise ValueError(f"request already contains question index {question_index}")
        missing = [index for index in range(1, question_index) if index not in indexed]
        if missing:
            raise ValueError(f"question index is not sequential; missing prior indices: {', '.join(map(str, missing))}")
    recorded_counts = {
        int(state.get("workflow_state", {}).get("expected_question_count"))
        for state in states
        if state.get("workflow_state", {}).get("expected_question_count") is not None
    }
    if len(recorded_counts) > 1:
        raise ValueError("request contains inconsistent expected question counts")
    if question_count is not None and recorded_counts and question_count not in recorded_counts:
        raise ValueError(
            f"question count {question_count} conflicts with recorded request count {next(iter(recorded_counts))}"
        )


def nearest_rule_files(vault_root: Path) -> list[Path]:
    result: list[Path] = []
    candidates = [vault_root / "AGENTS.md"]
    for parent in vault_root.parents:
        candidates.extend((parent / "AGENTS.md", parent / "ENVIRONMENT.md"))
    for path in candidates:
        if path.is_file() and path.resolve() not in {item.resolve() for item in result}:
            result.append(path)
    return result


def verification_runtime() -> dict[str, Any]:
    renderer = shutil.which("pdftoppm")
    return {
        "renderer": "pdftoppm" if renderer else None,
        "renderer_path": renderer,
        "policy": "one deterministic preparation attempt; never probe alternative PDF tools",
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    vault_root = args.vault_root.resolve()
    if not vault_root.is_dir():
        raise FileNotFoundError(f"vault root does not exist: {vault_root}")
    rules = []
    for path in nearest_rule_files(vault_root):
        content = path.read_text(encoding="utf-8", errors="replace")
        rules.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content[: args.max_rule_chars],
                "content_truncated": len(content) > args.max_rule_chars,
            }
        )
    skill_root = Path(__file__).resolve().parent.parent
    routing_path = next(
        (
            path
            for path in (
                skill_root / "config" / "domain-routing.json",
                skill_root / "config" / "intranet.json",
            )
            if path.is_file()
        ),
        None,
    )
    provider_path = skill_root / "config" / "retrieval-provider.json"
    return {
        "workflow": WORKFLOW,
        "vault_root": str(vault_root),
        "session": {
            "id": os.environ.get("HERMES_SESSION_ID"),
            "message_id": os.environ.get("HERMES_SESSION_MESSAGE_ID"),
            "platform": os.environ.get("HERMES_SESSION_PLATFORM"),
        },
        "required_rules": rules,
        "routing_config_path": str(routing_path) if routing_path else None,
        "routing": load_json(routing_path) if routing_path else None,
        "provider": load_json(provider_path) if provider_path.is_file() else None,
        "verification_runtime": verification_runtime(),
        "next_command": "begin",
    }


def elapsed_ms(started_monotonic_ns: int, started_wall_ns: int) -> float:
    current = time.monotonic_ns()
    elapsed = current - int(started_monotonic_ns)
    if elapsed < 0:
        elapsed = time.time_ns() - int(started_wall_ns)
    return round(max(0, elapsed) / 1_000_000, 3)


def timed_event(
    *,
    stage: str,
    route: str,
    started_at: str,
    started_monotonic_ns: int,
    started_wall_ns: int,
    summary: str,
    status: str = "completed",
    hit_count: int | None = None,
    inspected_paths: list[str] | None = None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "route": route,
        "status": status,
        "summary": summary,
        "hit_count": hit_count,
        "inspected_paths": inspected_paths or [],
        "evidence_ids": evidence_ids or [],
        "started_at": started_at,
        "ended_at": now_iso(),
        "duration_ms": elapsed_ms(started_monotonic_ns, started_wall_ns),
        "accounting": "primary",
    }


def begin(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    if not vault_root.is_dir():
        raise FileNotFoundError(f"vault root does not exist: {vault_root}")
    shape = validate_question_boundary(args)
    validate_request_sequence(vault_root, args.request_id, args.question_index, args.question_count)
    requires_verification = bool(args.verification_required)
    preflight_finished_monotonic = time.monotonic_ns()
    runtime_session_id = os.environ.get("HERMES_SESSION_ID") or args.session_id
    started = start_trace(
        vault_root,
        args.question,
        runtime_session_id,
        args.query_type,
        args.trace_id,
        args.request_id,
        args.question_index,
        WORKFLOW,
    )
    scope_started_at = now_iso()
    scope_started_monotonic = time.monotonic_ns()
    scope_started_wall = time.time_ns()
    scope = retrieve_scope(
        vault_root,
        args.question,
        top_k=args.top_k,
        top_documents=args.top_documents,
        top_sections=args.top_sections,
        provider_config=args.provider_config,
        trace_id=started["trace_id"],
    )
    state, _, _ = load_state(vault_root, started["trace_id"])
    state["session_message_id"] = os.environ.get("HERMES_SESSION_MESSAGE_ID")
    state["session_platform"] = os.environ.get("HERMES_SESSION_PLATFORM")
    existing_events = list(state.get("events", []))
    state["events"] = [
            clean_event(
                state,
                {
                    "stage": "query-preflight",
                    "route": "query-session",
                    "status": "completed",
                    "summary": "Validated the Vault and initialized the controlled-query session.",
                    "started_at": command_started_at,
                    "ended_at": state.get("created") or now_iso(),
                    "duration_ms": round(
                        max(0, preflight_finished_monotonic - command_started_monotonic) / 1_000_000,
                        3,
                    ),
                    "accounting": "primary",
                },
            ),
            *existing_events,
            clean_event(
                state,
                timed_event(
                    stage="scope-retrieval",
                    route="parallel-scope",
                    started_at=scope_started_at,
                    started_monotonic_ns=scope_started_monotonic,
                    started_wall_ns=scope_started_wall,
                    summary="Completed parallel scope retrieval and candidate fusion.",
                    status=str(scope.get("status") or "completed"),
                    hit_count=len(scope.get("candidates", [])),
                ),
            ),
        ]
    state["workflow_state"] = {
        "session_started_at": command_started_at,
        "session_started_monotonic_ns": command_started_monotonic,
        "session_started_wall_ns": command_started_wall,
        "candidate_review_started_at": now_iso(),
        "candidate_review_started_monotonic_ns": time.monotonic_ns(),
        "candidate_review_started_wall_ns": time.time_ns(),
        "command_count": 1,
        "evidence_catalog": {},
        "evidence_dirty": False,
        "question_shape": shape,
        "coupled_question": bool(args.coupled),
        "coupled_reason": str(args.coupled_reason or "").strip() or None,
        "expected_question_count": args.question_count,
        "verification_required": requires_verification,
        "verification_requirement_reason": "explicit CLI selection" if requires_verification else "not requested",
        "verification_catalog": {},
    }
    write_state(vault_root, state)
    return {
        "workflow": WORKFLOW,
        "trace": started,
        "scope": compact_result(scope, args.compact_limit),
        "next_command": "inspect",
    }


def fused_candidates(state: dict[str, Any]) -> list[dict[str, Any]]:
    for event in reversed(state.get("events", [])):
        if event.get("stage") == "candidate-fusion":
            return list(event.get("candidates", []))
    raise ValueError("trace has no candidate-fusion result")


def exact_projection_candidate(
    projections: dict[str, dict[str, Any]], document_path: str, section_id: str
) -> dict[str, Any] | None:
    normalized_document = document_path.replace("\\", "/").strip("/")
    projection = projections.get(normalized_document)
    if not projection:
        return None
    document = projection.get("document", {})
    for section in projection.get("sections", []):
        if str(section.get("section_id") or "") != section_id:
            continue
        return {
            "document_path": normalized_document,
            "source_filename": document.get("source_filename"),
            "section_id": section.get("section_id"),
            "title": section.get("title"),
            "path_titles": section.get("path_titles", []),
            "start_line": section.get("start_line"),
            "end_line": section.get("end_line"),
            "content_ranges": section.get("content_ranges", []),
            "pages": section.get("pages", []),
            "assets": section.get("assets", []),
            "quality": section.get("quality"),
            "ingest_status": section.get("ingest_status"),
            "viewer_url": section.get("viewer_url"),
            "retrieval_routes": ["projection-exact"],
            "route_ranks": {},
            "route_scores": {},
            "selection_origin": "projection-exact",
        }
    return None


def select_candidates(
    candidates: list[dict[str, Any]],
    selectors: list[str],
    projections: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    requested = selectors or [str(index) for index in range(1, min(3, len(candidates)) + 1)]
    selected: list[dict[str, Any]] = []
    for selector in requested:
        match: dict[str, Any] | None = None
        if selector.isdigit():
            rank = int(selector)
            if 1 <= rank <= len(candidates):
                match = candidates[rank - 1]
        else:
            document_part, separator, section_part = selector.partition("::")
            for candidate in candidates:
                if separator:
                    if (
                        str(candidate.get("document_path")) == document_part
                        and str(candidate.get("section_id")) == section_part
                    ):
                        match = candidate
                        break
                elif str(candidate.get("section_id")) == selector:
                    match = candidate
                    break
            if match is None and separator and projections is not None:
                match = exact_projection_candidate(projections, document_part, section_part)
        if match is None:
            raise ValueError(f"candidate selector did not match: {selector}")
        key = (str(match.get("document_path")), str(match.get("section_id")))
        if not any((str(item.get("document_path")), str(item.get("section_id"))) == key for item in selected):
            selected.append(match)
    return selected


def read_ranges(path: Path, ranges: list[dict[str, Any]], max_chars: int) -> tuple[str, bool]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    parts: list[str] = []
    for item in ranges:
        start = max(1, int(item.get("start_line") or 1))
        end = min(len(lines), int(item.get("end_line") or start))
        parts.append(f"<!-- lines {start}-{end} -->\n" + "\n".join(lines[start - 1 : end]))
    text = "\n\n".join(parts)
    return (text[:max_chars], len(text) > max_chars)


def block_id_for_ranges(ranges: list[dict[str, Any]]) -> str | None:
    spans = []
    for item in ranges:
        start = int(item.get("start_line") or 0)
        end = int(item.get("end_line") or start)
        if start > 0:
            spans.append(f"{start}-{max(start, end)}")
    return "lines-" + "+".join(spans) if spans else None


def verification_asset_paths(assets: list[dict[str, Any]]) -> list[str]:
    paths = []
    for asset in assets:
        value = asset.get("evidence_path") or asset.get("path")
        if value and Path(str(value)).suffix.casefold() in VISUAL_ASSET_SUFFIXES:
            paths.append(str(value))
    return list(dict.fromkeys(paths))


def register_evidence_packets(
    workflow: dict[str, Any],
    packets: list[dict[str, Any]],
    inspection_round: int,
) -> None:
    catalog = workflow.setdefault("evidence_catalog", {})
    existing = {
        (str(item.get("path")), str(item.get("section_id"))): handle
        for handle, item in catalog.items()
    }
    for packet in packets:
        key = (str(packet.get("document_path")), str(packet.get("section_id")))
        handle = existing.get(key)
        refreshed = {
            "path": packet.get("document_path"),
            "document_version": packet.get("document_version"),
            "section_id": packet.get("section_id"),
            "pages": packet.get("pages", []),
            "block_id": block_id_for_ranges(packet.get("content_ranges", [])),
            "original_asset_path": packet.get("source_path") if packet.get("source_exists") else None,
            "source_filename": packet.get("source_filename"),
            "viewer_url": packet.get("viewer_url"),
            "quality": packet.get("quality"),
            "ingest_status": packet.get("ingest_status"),
            "verification_assets": verification_asset_paths(packet.get("assets", [])),
        }
        if not handle:
            handle = f"P{len(catalog) + 1}"
            catalog[handle] = {
                "handle": handle,
                **refreshed,
                "inspection_rounds": [],
            }
            existing[key] = handle
        else:
            prior_version = str(catalog[handle].get("document_version") or "")
            current_version = str(packet.get("document_version") or "")
            if prior_version and current_version and prior_version != current_version:
                raise ValueError(
                    f"document changed between inspections for {handle}; start a new query trace"
                )
            catalog[handle].update(refreshed)
        rounds = catalog[handle].setdefault("inspection_rounds", [])
        if inspection_round not in rounds:
            rounds.append(inspection_round)
        packet["evidence_ref"] = handle


def source_map_frontmatter(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator:
            result[key.strip()] = value.strip().strip('"')
    return result


def ledger_outputs(ledger: dict[str, Any], section_id: str) -> list[str]:
    for section in ledger.get("sections", []):
        if str(section.get("id")) == section_id:
            return [str(item) for item in section.get("outputs", [])]
    return []


def resolve_source_path(
    vault_root: Path,
    document_meta: dict[str, Any],
    manifest: dict[str, Any],
    source_filename: str,
) -> Path | None:
    expected_sha256 = str(manifest.get("source", {}).get("sha256") or "").casefold()
    candidates: list[Path] = []
    for value in (document_meta.get("source_path"), manifest.get("source", {}).get("path")):
        if not value:
            continue
        path = Path(str(value))
        candidate = path if path.is_absolute() else vault_root / path
        if is_within(candidate, vault_root):
            candidates.append(candidate)
    if source_filename:
        candidates.append(vault_root / "10_Raw" / source_filename)

    def trusted(candidate: Path) -> bool:
        if not candidate.is_file() or not is_within(candidate, vault_root):
            return False
        return not expected_sha256 or file_sha256(candidate).casefold() == expected_sha256

    for candidate in candidates:
        if trusted(candidate):
            return candidate

    raw_root = vault_root / "10_Raw"
    if source_filename and raw_root.is_dir():
        matches = []
        for directory, directory_names, filenames in os.walk(raw_root):
            directory_names[:] = [name for name in directory_names if name.casefold() != "converted"]
            if source_filename not in filenames:
                continue
            candidate = Path(directory) / source_filename
            if trusted(candidate):
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
    return None


def asset_packet(
    vault_root: Path,
    bundle_path: Path,
    manifest: dict[str, Any],
    asset_ids: set[str],
    section_id: str,
    max_chars: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, entries in (("table", manifest.get("tables", [])), ("image", manifest.get("images", []))):
        for item in entries:
            asset_id = str(item.get("id") or "")
            if asset_id not in asset_ids and str(item.get("section_id") or "") != section_id:
                continue
            record = {
                "type": kind,
                "id": asset_id,
                "caption": item.get("caption"),
                "page_start": item.get("page_start", item.get("page")),
                "page_end": item.get("page_end", item.get("page")),
                "bbox": item.get("bbox"),
                "quality": item.get("quality"),
                "path": None,
                "evidence_path": None,
            }
            asset_path = bundle_path / str(item.get("path") or "")
            if asset_path.is_file():
                record["path"] = vault_relative(asset_path, vault_root)
                if asset_path.suffix.casefold() == ".md":
                    content = asset_path.read_text(encoding="utf-8", errors="replace")
                    record["content"] = content[:max_chars]
                    record["content_truncated"] = len(content) > max_chars
            evidence_value = item.get("evidence_path")
            if evidence_value:
                evidence_path = bundle_path / str(evidence_value)
                if evidence_path.is_file():
                    record["evidence_path"] = vault_relative(evidence_path, vault_root)
            result.append(record)
    return result


def build_evidence_packet(
    vault_root: Path,
    candidate: dict[str, Any],
    projection: dict[str, Any],
    max_chars: int,
) -> tuple[dict[str, Any], dict[str, float], list[str]]:
    timings = {"document_reading": 0.0, "table_figure_resolution": 0.0, "provenance_resolution": 0.0}
    inspected: list[str] = []
    provenance_started = time.monotonic_ns()
    document_meta = projection.get("document", {})
    document_path = vault_root / str(document_meta.get("document_path") or candidate.get("document_path") or "")
    bundle_path = vault_root / str(document_meta.get("bundle_path") or document_path.parent)
    manifest_path = bundle_path / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {}
    ledger_value = projection.get("ledger_path")
    ledger_path = vault_root / str(ledger_value) if ledger_value else None
    ledger = load_json(ledger_path) if ledger_path and ledger_path.is_file() else {}
    source_filename = str(document_meta.get("source_filename") or candidate.get("source_filename") or "")
    source_path = resolve_source_path(vault_root, document_meta, manifest, source_filename)
    external_source_paths = []
    for value in (document_meta.get("source_path"), manifest.get("source", {}).get("path")):
        if not value:
            continue
        path = Path(str(value))
        if path.is_absolute() and not is_within(path, vault_root):
            external_source_paths.append(path.as_posix())
    source_map_path = (
        ledger_path.with_name(ledger_path.name.replace(".section-ledger.json", ".source-map.md"))
        if ledger_path
        else vault_root / "_system" / "reports" / f"{Path(source_filename).stem}.source-map.md"
    )
    timings["provenance_resolution"] += (time.monotonic_ns() - provenance_started) / 1_000_000

    reading_started = time.monotonic_ns()
    content, truncated = read_ranges(document_path, candidate.get("content_ranges", []), max_chars)
    timings["document_reading"] += (time.monotonic_ns() - reading_started) / 1_000_000
    inspected.append(vault_relative(document_path, vault_root))

    assets_started = time.monotonic_ns()
    assets = asset_packet(
        vault_root,
        bundle_path,
        manifest,
        {str(item) for item in candidate.get("assets", [])},
        str(candidate.get("section_id") or ""),
        max_chars,
    )
    timings["table_figure_resolution"] += (time.monotonic_ns() - assets_started) / 1_000_000
    inspected.extend(str(item[key]) for item in assets for key in ("path", "evidence_path") if item.get(key))

    governed: list[dict[str, Any]] = []
    for output in ledger_outputs(ledger, str(candidate.get("section_id") or "")):
        if not output.startswith(("30_Cards/", "40_Concepts/", "50_Projects/")):
            continue
        output_path = vault_root / output
        if output_path.is_file():
            text = output_path.read_text(encoding="utf-8", errors="replace")
            governed.append({"path": output, "content": text[:max_chars], "content_truncated": len(text) > max_chars})
            inspected.append(output)

    if ledger_path and ledger_path.is_file():
        inspected.append(vault_relative(ledger_path, vault_root))
    if source_map_path.is_file():
        inspected.append(vault_relative(source_map_path, vault_root))
    if manifest_path.is_file():
        inspected.append(vault_relative(manifest_path, vault_root))
    routing = document_meta.get("routing_metadata") or {}
    viewer_url = candidate.get("viewer_url") or (
        routing.get("viewer_url") if isinstance(routing, dict) else None
    )
    document_version = (
        projection.get("source_state", {}).get("document_hash")
        or manifest.get("source", {}).get("sha256")
        or file_sha256(document_path)
    )
    packet = {
        "document_path": vault_relative(document_path, vault_root),
        "source_filename": source_filename,
        "source_path": vault_relative(source_path, vault_root) if source_path else "unresolved",
        "source_exists": bool(source_path),
        "source_sha256": manifest.get("source", {}).get("sha256"),
        "document_version": document_version,
        "section_id": candidate.get("section_id"),
        "title": candidate.get("title"),
        "pages": candidate.get("pages", []),
        "quality": candidate.get("quality"),
        "ingest_status": candidate.get("ingest_status"),
        "content_ranges": candidate.get("content_ranges", []),
        "content": content,
        "content_truncated": truncated,
        "assets": assets,
        "governed_artifacts": governed,
        "control": {
            "manifest_path": vault_relative(manifest_path, vault_root) if manifest_path.is_file() else None,
            "ledger_path": vault_relative(ledger_path, vault_root) if ledger_path and ledger_path.is_file() else None,
            "source_map_path": vault_relative(source_map_path, vault_root) if source_map_path.is_file() else None,
            "source_map": source_map_frontmatter(source_map_path),
            "source_state": projection.get("source_state", {}),
            "external_source_paths": list(dict.fromkeys(external_source_paths)),
        },
        "viewer_url": viewer_url,
        "selection_origin": candidate.get("selection_origin", "fused-candidate"),
    }
    return packet, timings, inspected


def packet_verification_readiness(packet: dict[str, Any]) -> dict[str, Any]:
    image_paths = verification_asset_paths(packet.get("assets", []))
    if image_paths:
        return {"status": "ready", "mode": "evidence-image", "paths": image_paths}
    if packet.get("viewer_url"):
        return {"status": "ready", "mode": "viewer", "viewer_url": packet.get("viewer_url")}
    runtime = verification_runtime()
    if packet.get("source_exists") and packet.get("pages") and runtime.get("renderer"):
        return {"status": "preparable", "mode": runtime["renderer"], "paths": []}
    return {
        "status": "unavailable",
        "mode": None,
        "paths": [],
        "reason": "No evidence image/viewer is registered and the deterministic pdftoppm renderer is unavailable.",
        "recommended_evidence_level": "needs-qa",
    }


def compact_evidence_packet(packet: dict[str, Any]) -> dict[str, Any]:
    source_map = packet.get("control", {}).get("source_map", {})
    assets = []
    for asset in packet.get("assets", []):
        assets.append(
            {
                key: asset.get(key)
                for key in (
                    "type",
                    "id",
                    "caption",
                    "page_start",
                    "page_end",
                    "bbox",
                    "quality",
                    "path",
                    "evidence_path",
                    "content",
                    "content_truncated",
                )
                if asset.get(key) is not None
            }
        )
    return {
        "evidence_ref": packet.get("evidence_ref"),
        "title": packet.get("title"),
        "section_id": packet.get("section_id"),
        "pages": packet.get("pages", []),
        "content": packet.get("content"),
        "content_truncated": packet.get("content_truncated"),
        "source_filename": packet.get("source_filename"),
        "source_path": packet.get("source_path"),
        "source_exists": packet.get("source_exists"),
        "document_path": packet.get("document_path"),
        "document_version": packet.get("document_version"),
        "assets": assets,
        "governed_artifacts": packet.get("governed_artifacts", []),
        "qa": {
            "quality": packet.get("quality"),
            "ingest_status": packet.get("ingest_status"),
            "source_map_validation_status": source_map.get("validation_status"),
        },
        "viewer_url": packet.get("viewer_url"),
        "selection_origin": packet.get("selection_origin"),
        "verification": packet_verification_readiness(packet),
    }


HARD_FAILURE_STATUSES = {"fail", "failed", "unavailable", "error", "invalid"}


def failed_status(value: Any) -> bool:
    return str(value or "").strip().casefold() in HARD_FAILURE_STATUSES


def packet_has_substantive_content(packet: dict[str, Any]) -> bool:
    content = str(packet.get("content") or "")
    substantive_lines = [
        line
        for line in content.splitlines()
        if line.strip()
        and not re.match(r"^\s{0,3}#{1,6}\s+\S+", line)
        and line.strip() not in {"---", "***", "___"}
    ]
    if substantive_lines:
        return True
    return any(str(asset.get("content") or "").strip() for asset in packet.get("assets", []))


def evidence_level_contract(packets: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate non-blocking diagnostics from evidence-chain hard blockers."""
    blocked: list[str] = []
    diagnostics: list[str] = []
    all_packet_quality_pass = True
    for packet in packets:
        evidence_ref = str(packet.get("evidence_ref") or "unregistered")
        quality = str(packet.get("quality") or "missing").strip().casefold()
        source_map_status = str(
            packet.get("control", {}).get("source_map", {}).get("validation_status") or "missing"
        ).strip().casefold()
        ingest_status = str(packet.get("ingest_status") or "missing").strip().casefold()
        all_packet_quality_pass = all_packet_quality_pass and quality == "pass"
        if quality != "pass":
            diagnostics.append(f"{evidence_ref}:quality={quality}")
        if failed_status(quality):
            blocked.append(f"{evidence_ref}:quality={quality}")
        if source_map_status != "pass":
            diagnostics.append(f"{evidence_ref}:source-map-validation={source_map_status}")
        if failed_status(source_map_status):
            blocked.append(f"{evidence_ref}:source-map-validation={source_map_status}")
        if ingest_status not in {"ingested", "complete", "completed", "pass"}:
            diagnostics.append(f"{evidence_ref}:ingest-status={ingest_status}")
        if not packet_has_substantive_content(packet):
            blocked.append(f"{evidence_ref}:no-substantive-content")
        if not packet.get("source_exists"):
            blocked.append(f"{evidence_ref}:original-source-unresolved")
        if not packet.get("pages"):
            blocked.append(f"{evidence_ref}:original-pages-unresolved")
        if packet.get("content_truncated"):
            blocked.append(f"{evidence_ref}:content-truncated")
        for asset in packet.get("assets", []):
            asset_quality = str(asset.get("quality") or "").strip().casefold()
            if asset_quality and asset_quality != "pass":
                asset_id = str(asset.get("id") or asset.get("type") or "asset")
                diagnostic = f"{evidence_ref}:{asset_id}-quality={asset_quality}"
                diagnostics.append(diagnostic)
                if failed_status(asset_quality):
                    blocked.append(diagnostic)
    direct_use_allowed = not blocked
    return {
        "ordinary_pass_quality": all_packet_quality_pass and direct_use_allowed,
        "direct_use_allowed": direct_use_allowed,
        "full_reference_required": False,
        "reference_read_policy": (
            "do not read references/evidence-levels.md for packet statuses; exclude blocked packet refs and use the inline rules"
            if blocked
            else "do not read references/evidence-levels.md; non-failed diagnostics remain usable as source-backed evidence"
        ),
        "blocked_conditions": list(dict.fromkeys(blocked)),
        "non_blocking_diagnostics": list(dict.fromkeys(diagnostics)),
        "inline_rules": {
            "source-backed": "non-failed converted evidence with substantive content resolves to original source and page; warn, pending, qa_required, ambiguous, or incomplete metadata may be qualified but is not a read blocker",
            "clear": "a governed conclusion or otherwise clearly durable pass-quality source supports the claim and resolves to original source and page",
            "needs-qa": "a hard blocker, actual source conflict, or explicitly required incomplete verification affects the claim",
            "gap": "substantive original-source evidence is unavailable",
        },
        "model_escalation_triggers": [
            "actual source conflict",
            "the answer cannot be determined from the visible substantive content",
            "explicitly required visual verification is incomplete",
        ],
    }


def inspect(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    state, _, _ = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
    workflow = state.setdefault("workflow_state", {})
    inspection_count = int(workflow.get("inspection_count") or 0)
    if inspection_count >= MAX_INSPECTIONS:
        summary = (
            f"Inspection limit reached ({MAX_INSPECTIONS}); finalize this trace as completed when existing "
            "evidence is sufficient, otherwise finalize it as incomplete with an unresolved item."
        )
        if not any(
            event.get("stage") == "query-guardrail" and event.get("route") == "inspection-limit"
            for event in state.get("events", [])
        ):
            state.setdefault("events", []).append(
                clean_event(
                    state,
                    {
                        "stage": "query-guardrail",
                        "route": "inspection-limit",
                        "status": "blocked",
                        "summary": summary,
                        "started_at": command_started_at,
                        "ended_at": now_iso(),
                        "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
                    },
                )
            )
            workflow["command_count"] = int(workflow.get("command_count") or 1) + 1
            write_state(vault_root, state)
        return {
            "workflow": WORKFLOW,
            "trace_id": args.trace_id,
            "status": "blocked",
            "reason": summary,
            "next_command": "finalize",
            "required_status_when_evidence_is_insufficient": "incomplete",
        }
    if inspection_count:
        review_stage = "evidence-gap-review"
        review_route = "supplemental-selection" if workflow.get("evidence_dirty") else "agent-follow-up-selection"
        review_summary = "Inspected additional candidates to close a recorded evidence gap."
        review_started_at = workflow.get("evidence_gap_review_started_at") or workflow.get("answer_synthesis_started_at")
        review_started_monotonic = (
            workflow.get("evidence_gap_review_started_monotonic_ns")
            or workflow.get("answer_synthesis_started_monotonic_ns")
        )
        review_started_wall = (
            workflow.get("evidence_gap_review_started_wall_ns")
            or workflow.get("answer_synthesis_started_wall_ns")
        )
    else:
        review_stage = "candidate-review"
        review_route = "agent-selection"
        review_summary = "Selected fused candidates for one batched evidence inspection."
        review_started_at = workflow.get("candidate_review_started_at")
        review_started_monotonic = workflow.get("candidate_review_started_monotonic_ns")
        review_started_wall = workflow.get("candidate_review_started_wall_ns")
    candidate_review = timed_event(
        stage=review_stage,
        route=review_route,
        started_at=str(review_started_at or state.get("updated") or command_started_at),
        started_monotonic_ns=int(review_started_monotonic or command_started_monotonic),
        started_wall_ns=int(review_started_wall or command_started_wall),
        summary=review_summary,
    )
    projections = load_projections(vault_root)
    selected = select_candidates(fused_candidates(state), args.candidate, projections)
    packets: list[dict[str, Any]] = []
    timings = {"document_reading": 0.0, "table_figure_resolution": 0.0, "provenance_resolution": 0.0}
    inspected_paths: list[str] = []
    for candidate in selected:
        projection = projections.get(str(candidate.get("document_path") or ""))
        if not projection:
            raise ValueError(f"query projection missing for {candidate.get('document_path')}")
        packet, packet_timings, packet_paths = build_evidence_packet(
            vault_root, candidate, projection, args.max_chars_per_section
        )
        packets.append(packet)
        for key, value in packet_timings.items():
            timings[key] += value
        inspected_paths.extend(packet_paths)
    register_evidence_packets(workflow, packets, inspection_count + 1)
    ended_at = now_iso()
    state.setdefault("events", []).extend(
        [
            clean_event(state, candidate_review),
            clean_event(
                state,
                {
                    "stage": "document-reading",
                    "route": "converted-source",
                    "status": "completed" if packets else "empty",
                    "summary": f"Read {len(packets)} complete selected source sections in one batch.",
                    "hit_count": len(packets),
                    "duration_ms": round(timings["document_reading"], 3),
                    "started_at": command_started_at,
                    "ended_at": ended_at,
                    "inspected_paths": sorted(set(packet["document_path"] for packet in packets)),
                },
            ),
            clean_event(
                state,
                {
                    "stage": "table-figure-resolution",
                    "route": "bundle-assets",
                    "status": "completed" if any(packet["assets"] for packet in packets) else "skipped",
                    "summary": "Resolved related table/figure metadata and available verification assets.",
                    "hit_count": sum(len(packet["assets"]) for packet in packets),
                    "duration_ms": round(timings["table_figure_resolution"], 3),
                    "started_at": command_started_at,
                    "ended_at": ended_at,
                    "inspected_paths": sorted(
                        {str(item[key]) for packet in packets for item in packet["assets"] for key in ("path", "evidence_path") if item.get(key)}
                    ),
                },
            ),
            clean_event(
                state,
                {
                    "stage": "provenance-resolution",
                    "route": "control-metadata",
                    "status": "completed",
                    "summary": "Resolved original PDF identity, pages, Bundle manifest, ledger, and source-map metadata.",
                    "hit_count": len(packets),
                    "duration_ms": round(timings["provenance_resolution"], 3),
                    "started_at": command_started_at,
                    "ended_at": ended_at,
                    "inspected_paths": sorted(set(inspected_paths)),
                },
            ),
        ]
    )
    workflow.update(
        {
            "answer_synthesis_started_at": now_iso(),
            "answer_synthesis_started_monotonic_ns": time.monotonic_ns(),
            "answer_synthesis_started_wall_ns": time.time_ns(),
            "command_count": int(workflow.get("command_count") or 1) + 1,
            "inspection_count": inspection_count + 1,
            "evidence_dirty": False,
            "supplemental_reason": None,
            "evidence_gap_review_started_at": None,
            "evidence_gap_review_started_monotonic_ns": None,
            "evidence_gap_review_started_wall_ns": None,
        }
    )
    write_state(vault_root, state)
    verification_required = bool(workflow.get("verification_required"))
    verification_contract = {
        "verification_required": verification_required,
        "inspect_grants_verified_status": False,
        "verified_evidence_refs_policy": (
            "only refs whose registered carrier was visually checked after verify returned ready"
            if verification_required
            else "must be empty because visual verification was not requested"
        ),
        "required_verified_evidence_refs": None if verification_required else [],
        "page_asset_verification_event_policy": (
            "required for each verified ref, with inspected_paths"
            if verification_required
            else "omit because visual verification was not requested"
        ),
    }
    level_contract = evidence_level_contract(packets)
    event_contract = {
        "ordinary_events": [] if not verification_required else None,
        "policy": (
            "set events to []; query-session already records inspect, search, reading, and provenance"
            if not verification_required
            else "omit inspect/search events; add only an actual completed page-asset-verification event"
        ),
        "evidence_ref_policy": "never add a claim or evidence ref solely to make an optional event reference valid",
    }
    return {
        "workflow": WORKFLOW,
        "trace_id": args.trace_id,
        "selected_count": len(packets),
        "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
        "evidence_packets": [compact_evidence_packet(packet) for packet in packets],
        "finalize_contract": {
            "top_level_fields": sorted(DECISION_KEYS - {"unresolved_items"}),
            "top_level_aliases": {"unresolved_items": "unresolved"},
            "claim_fields": sorted(CLAIM_KEYS),
            "verification_contract": verification_contract,
            "evidence_level_contract": level_contract,
            "event_submission_contract": event_contract,
            "claim_set_policy": (
                "Use the minimum sufficient claim set: each claim must answer a requested output attribute or "
                "action; subject qualifiers only narrow scope and do not create claims. Merge closely related "
                "parameters supported by the same evidence."
            ),
            "claim_pruning_gate": (
                "Before finalize, remove any claim whose deletion still leaves every requested output answered. "
                "Evidence availability never creates answer scope. Put unrequested comparison, background, "
                "applicability, or operational context only in a necessary qualification on a requested claim; "
                "otherwise omit it."
            ),
            "qualification_policy": (
                "Attach scope or evidence boundaries briefly to the affected claim; do not create a separate "
                "background or applicability claim unless the question asks for it."
            ),
            "unresolved_policy": "Keep only unresolved items that materially change correctness or use of the answer.",
            "conclusion_policy": "Use one short synthesis and do not repeat the claims item by item.",
            "event_standard_fields": sorted(EVENT_KEYS - {"extensions"}),
            "event_extension_policy": (
                "Unknown event fields are preserved under extensions; they never satisfy a stage or evidence gate. "
                "Omit events unless they add an actual audit or verification fact."
            ),
        },
        "next_command": "verify" if verification_required else "finalize",
    }


def candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (str(candidate.get("document_path") or ""), str(candidate.get("section_id") or ""))


def supplement(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    state, _, _ = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
    workflow = state.setdefault("workflow_state", {})
    if int(workflow.get("inspection_count") or 0) < 1:
        raise ValueError("supplement requires an initial inspect")
    supplement_count = int(workflow.get("supplement_count") or 0)
    if supplement_count >= MAX_SUPPLEMENTS or int(workflow.get("inspection_count") or 0) >= MAX_INSPECTIONS:
        summary = (
            f"Supplement limit reached ({MAX_SUPPLEMENTS}) or no inspection slot remains; finalize this trace "
            "as completed when existing evidence is sufficient, otherwise finalize it as incomplete."
        )
        if not any(
            event.get("stage") == "query-guardrail" and event.get("route") == "supplement-limit"
            for event in state.get("events", [])
        ):
            state.setdefault("events", []).append(
                clean_event(
                    state,
                    {
                        "stage": "query-guardrail",
                        "route": "supplement-limit",
                        "status": "blocked",
                        "summary": summary,
                        "started_at": command_started_at,
                        "ended_at": now_iso(),
                        "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
                    },
                )
            )
            workflow["command_count"] = int(workflow.get("command_count") or 1) + 1
            write_state(vault_root, state)
        return {
            "workflow": WORKFLOW,
            "trace_id": args.trace_id,
            "status": "blocked",
            "reason": summary,
            "next_command": "finalize",
            "required_status_when_evidence_is_insufficient": "incomplete",
        }
    if workflow.get("evidence_dirty"):
        raise ValueError("the previous supplement must be inspected before another supplement")
    scope = retrieve_scope(
        vault_root,
        args.query,
        top_k=args.top_k,
        top_documents=args.top_documents,
        top_sections=args.top_sections,
        provider_config=args.provider_config,
    )
    previous = fused_candidates(state)
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in [*scope.get("candidates", []), *previous]:
        key = candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        merged.append(candidate)
    ended_at = now_iso()
    state.setdefault("events", []).extend(
        [
            clean_event(
                state,
                {
                    "stage": "supplemental-retrieval",
                    "route": "parallel-scope",
                    "status": scope.get("status"),
                    "summary": f"Searched for a recorded evidence gap: {args.reason}",
                    "hit_count": len(scope.get("candidates", [])),
                    "duration_ms": scope.get("duration_ms"),
                    "started_at": command_started_at,
                    "ended_at": ended_at,
                    "candidates": scope.get("candidates", []),
                },
            ),
            clean_event(
                state,
                {
                    "stage": "candidate-fusion",
                    "route": "supplemental-fusion",
                    "status": "ok" if merged else "empty",
                    "summary": f"Merged supplemental scope with the prior scope; retained {len(merged)} candidates.",
                    "hit_count": len(merged),
                    "duration_ms": 0.0,
                    "started_at": ended_at,
                    "ended_at": ended_at,
                    "accounting": "diagnostic",
                    "candidates": merged,
                },
            ),
        ]
    )
    gap_review_started_at = now_iso()
    gap_review_started_monotonic = time.monotonic_ns()
    gap_review_started_wall = time.time_ns()
    workflow.update(
        {
            "evidence_dirty": True,
            "supplement_count": supplement_count + 1,
            "supplemental_reason": args.reason,
            "evidence_gap_review_started_at": gap_review_started_at,
            "evidence_gap_review_started_monotonic_ns": gap_review_started_monotonic,
            "evidence_gap_review_started_wall_ns": gap_review_started_wall,
            "command_count": int(workflow.get("command_count") or 2) + 1,
        }
    )
    write_state(vault_root, state)
    compact_scope = dict(scope)
    compact_scope["candidates"] = merged
    return {
        "workflow": WORKFLOW,
        "trace_id": args.trace_id,
        "reason": args.reason,
        "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
        "scope": compact_result(compact_scope, args.compact_limit),
        "next_command": "inspect",
    }


def render_pdf_pages(
    vault_root: Path,
    note_path: Path,
    trace_id: str,
    handle: str,
    source_path: str,
    pages: list[int],
) -> list[str]:
    renderer = shutil.which("pdftoppm")
    if not renderer:
        return []
    pdf_path = (vault_root / source_path).resolve()
    pdf_path.relative_to(vault_root)
    if not pdf_path.is_file():
        return []
    output_root = note_path.parent / "_verification" / trace_id
    output_root.mkdir(parents=True, exist_ok=True)
    rendered: list[str] = []
    for page in sorted(set(int(value) for value in pages if int(value) > 0))[:4]:
        prefix = output_root / f"{handle}-page-{page}"
        completed = subprocess.run(
            [renderer, "-f", str(page), "-l", str(page), "-png", "-singlefile", str(pdf_path), str(prefix)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output_path = prefix.with_suffix(".png")
        if completed.returncode != 0 or not output_path.is_file():
            raise RuntimeError(
                f"pdftoppm failed for {handle} page {page}: {(completed.stderr or completed.stdout).strip()}"
            )
        rendered.append(vault_relative(output_path, vault_root))
    return rendered


def prepare_verification(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    state, _, note_path = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
    if state.get("status") != "in_progress":
        raise ValueError(f"query trace is not in progress: {state.get('status')}")
    workflow = state.setdefault("workflow_state", {})
    catalog = workflow.get("evidence_catalog", {})
    requested = [str(value) for value in args.evidence_ref]
    if not requested:
        raise ValueError("verify requires at least one --evidence-ref")
    unknown = [handle for handle in requested if handle not in catalog]
    if unknown:
        raise ValueError(f"verify references unknown evidence handles: {', '.join(unknown)}")
    results = []
    for handle in requested:
        entry = catalog[handle]
        paths = [
            path
            for path in (str(value) for value in entry.get("verification_assets", []))
            if (vault_root / path).is_file()
        ]
        mode = "evidence-image" if paths else None
        status = "ready" if paths else "unavailable"
        reason = None
        if not paths and entry.get("viewer_url"):
            mode = "viewer"
            status = "ready"
        elif not paths and entry.get("original_asset_path") and entry.get("pages"):
            try:
                paths = render_pdf_pages(
                    vault_root,
                    note_path,
                    args.trace_id,
                    handle,
                    str(entry["original_asset_path"]),
                    [int(value) for value in entry.get("pages", [])],
                )
                if paths:
                    mode = "pdftoppm"
                    status = "ready"
                else:
                    reason = "The deterministic pdftoppm renderer is unavailable and no evidence image/viewer is registered."
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                status = "failed"
                reason = str(exc)
        else:
            reason = "No original PDF pages, evidence image, or viewer is registered for this evidence packet."
        result = {
            "evidence_ref": handle,
            "status": status,
            "mode": mode,
            "paths": paths,
            "viewer_url": entry.get("viewer_url") if mode == "viewer" else None,
            "reason": reason,
            "recommended_evidence_level": "clear-after-visual-check" if status == "ready" else "needs-qa",
            "required_unresolved": None
            if status == "ready"
            else f"{handle} original-page verification unavailable: {reason}",
        }
        workflow.setdefault("verification_catalog", {})[handle] = result
        results.append(result)
    ended_at = now_iso()
    state.setdefault("events", []).append(
        clean_event(
            state,
            {
                "stage": "verification-readiness",
                "route": "deterministic-verification",
                "status": "ready" if all(item["status"] == "ready" for item in results) else "unavailable",
                "summary": "Prepared registered verification carriers without probing alternative PDF tools.",
                "hit_count": sum(len(item["paths"]) + bool(item.get("viewer_url")) for item in results),
                "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
                "started_at": command_started_at,
                "ended_at": ended_at,
                "accounting": "diagnostic",
                "inspected_paths": [path for item in results for path in item["paths"]],
            },
        )
    )
    workflow["command_count"] = int(workflow.get("command_count") or 2) + 1
    write_state(vault_root, state)
    return {
        "workflow": WORKFLOW,
        "trace_id": args.trace_id,
        "verification": results,
        "stopping_rule": "Do not probe pdftotext, Python PDF libraries, other binaries, Bundle listings, or converted text.",
        "next_command": "visually-check-ready-carrier-then-finalize",
    }


def referenced_packet_handles(claims: list[dict[str, Any]]) -> list[str]:
    handles: list[str] = []
    for claim in claims:
        for value in claim.get("evidence_refs", []):
            handle = str(value)
            if handle not in handles:
                handles.append(handle)
    return handles


def reject_unknown_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(decision, DECISION_KEYS, "decision")
    normalized = dict(decision)
    if "unresolved_items" in normalized:
        if "unresolved" in normalized and normalized["unresolved"] != normalized["unresolved_items"]:
            raise ValueError("decision unresolved and unresolved_items must not conflict")
        normalized["unresolved"] = normalized.pop("unresolved_items")
    normalized.setdefault("unresolved", [])
    if not isinstance(normalized["unresolved"], list) or not all(
        isinstance(item, str) and item.strip() for item in normalized["unresolved"]
    ):
        raise ValueError("decision unresolved must be a list of non-empty strings")
    claims = normalized.get("claims", [])
    if not isinstance(claims, list):
        raise ValueError("decision claims must be a list")
    for index, claim in enumerate(claims, start=1):
        if not isinstance(claim, dict):
            raise ValueError(f"decision claim {index} must be an object")
        reject_unknown_keys(claim, CLAIM_KEYS, f"decision claim {index}")
        resolve_claim_text(claim, f"decision claim {index}")
        refs = claim.get("evidence_refs", [])
        if not isinstance(refs, list):
            raise ValueError(f"decision claim {index} evidence_refs must be a list")
    events = normalized.get("events", [])
    if not isinstance(events, list):
        raise ValueError("decision events must be a list")
    normalized_events: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise ValueError(f"decision event {index} must be an object")
        extensions = event.get("extensions", {})
        if extensions is None:
            extensions = {}
        if not isinstance(extensions, dict):
            raise ValueError(f"decision event {index} extensions must be an object")
        reserved_extensions = sorted(set(extensions) & EVENT_KEYS)
        if reserved_extensions:
            raise ValueError(
                f"decision event {index} extensions contain reserved fields: {', '.join(reserved_extensions)}"
            )
        normalized_event = {key: value for key, value in event.items() if key in EVENT_KEYS and key != "extensions"}
        for list_field in ("evidence_refs", "inspected_paths"):
            if list_field in normalized_event and not isinstance(normalized_event[list_field], list):
                raise ValueError(f"decision event {index} {list_field} must be a list")
        merged_extensions = dict(extensions)
        for key, value in event.items():
            if key in EVENT_KEYS:
                continue
            if key in merged_extensions and merged_extensions[key] != value:
                raise ValueError(f"decision event {index} extension conflicts with field: {key}")
            merged_extensions[key] = value
        if merged_extensions:
            normalized_event["extensions"] = merged_extensions
        normalized_events.append(normalized_event)
    normalized["events"] = normalized_events
    verified = normalized.get("verified_evidence_refs", [])
    if not isinstance(verified, list):
        raise ValueError("decision verified_evidence_refs must be a list")
    return normalized


def validate_verification_decision(
    workflow: dict[str, Any],
    decision: dict[str, Any],
    handles: list[str],
    verified: set[str],
) -> None:
    verification_catalog = workflow.get("verification_catalog", {})
    if not workflow.get("verification_required") and verified:
        raise ValueError(
            "verified_evidence_refs must be empty when visual verification was not requested; "
            "inspect reads evidence but does not grant verified status"
        )
    if workflow.get("verification_required"):
        missing_readiness = sorted(handle for handle in handles if handle not in verification_catalog)
        if missing_readiness:
            raise ValueError(
                f"evidence marked as requiring visual verification must run verify once before finalize: "
                f"{', '.join(missing_readiness)}"
            )
    unavailable = sorted(
        handle
        for handle in verified
        if verification_catalog.get(handle, {}).get("status") in {"unavailable", "failed"}
    )
    if unavailable:
        raise ValueError(f"cannot mark unavailable verification handles as verified: {', '.join(unavailable)}")
    verification_events = [
        event
        for event in decision.get("events", [])
        if event.get("stage") == "page-asset-verification" and event.get("status", "completed") == "completed"
    ]
    for handle in verified:
        matching = [event for event in verification_events if handle in {str(ref) for ref in event.get("evidence_refs", [])}]
        if not matching or not any(event.get("inspected_paths") for event in matching):
            raise ValueError(
                f"verified evidence {handle} requires a completed page-asset-verification event with inspected_paths"
            )
        readiness = verification_catalog.get(handle)
        if readiness:
            allowed = {
                str(value)
                for value in [*readiness.get("paths", []), readiness.get("viewer_url")]
                if value
            }
            inspected = {str(path) for event in matching for path in event.get("inspected_paths", [])}
            if allowed and not inspected.intersection(allowed):
                raise ValueError(f"verified evidence {handle} did not inspect a registered verification carrier")
    if not workflow.get("verification_required"):
        return
    unchecked = sorted(set(handles) - verified)
    evidence_level = str(decision.get("evidence_level") or "source-backed")
    if unchecked and evidence_level in {"clear", "source-backed"}:
        raise ValueError(
            f"evidence marked as requiring visual verification must be checked before {evidence_level}: "
            f"{', '.join(unchecked)}"
        )
    if unchecked and evidence_level == "needs-qa" and not decision.get("unresolved"):
        raise ValueError("unverified evidence marked as requiring visual verification needs a non-empty unresolved item")


def decision_to_manifest(state: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    decision = normalize_decision(decision)
    workflow = state.get("workflow_state", {})
    if workflow.get("evidence_dirty"):
        raise ValueError("supplemental retrieval must be followed by inspect before finalize")
    catalog = workflow.get("evidence_catalog", {})
    claims = decision.get("claims", [])
    handles = referenced_packet_handles(claims)
    missing = [handle for handle in handles if handle not in catalog]
    if missing:
        raise ValueError(f"claims reference uninspected evidence handles: {', '.join(missing)}")
    verified = {str(item) for item in decision.get("verified_evidence_refs", [])}
    missing_verified = sorted(verified - set(catalog))
    if missing_verified:
        raise ValueError(f"verification references unknown evidence handles: {', '.join(missing_verified)}")
    unused_verified = sorted(verified - set(handles))
    if unused_verified:
        raise ValueError(f"verification references handles not used by a claim: {', '.join(unused_verified)}")
    validate_verification_decision(workflow, decision, handles, verified)
    handle_to_evidence = {handle: f"E{index}" for index, handle in enumerate(handles, start=1)}
    evidence: list[dict[str, Any]] = []
    for handle in handles:
        entry = catalog[handle]
        asset_path = entry.get("original_asset_path")
        asset_status = "verified" if handle in verified else "not-checked"
        evidence.append(
            {
                "evidence_id": handle_to_evidence[handle],
                "evidence_ref": handle,
                "path": entry.get("path"),
                "document_version": entry.get("document_version"),
                "section_id": entry.get("section_id"),
                "pages": entry.get("pages", []),
                "block_id": entry.get("block_id"),
                "original_asset_status": asset_status,
                "original_asset_path": asset_path,
                "source_filename": entry.get("source_filename"),
                "viewer_url": entry.get("viewer_url"),
                "summary": f"Inspected {handle} and inherited its governed provenance metadata.",
            }
        )
    normalized_claims: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        refs = [str(item) for item in claim.get("evidence_refs", [])]
        normalized_claims.append(
            {
                "claim_id": f"C{index}",
                "text": resolve_claim_text(claim, f"decision claim {index}"),
                "status": claim.get("status", "supported"),
                "evidence_ids": [handle_to_evidence[item] for item in refs],
                "qualification": claim.get("qualification"),
            }
        )
    events: list[dict[str, Any]] = []
    for event in decision.get("events", []):
        item = dict(event)
        refs = [str(value) for value in item.pop("evidence_refs", [])]
        unknown = [value for value in refs if value not in handle_to_evidence]
        if unknown:
            raise ValueError(f"event references unused evidence handles: {', '.join(unknown)}")
        item["evidence_ids"] = [handle_to_evidence[value] for value in refs]
        events.append(item)
    conclusion = str(decision.get("conclusion") or "")
    if not conclusion and normalized_claims:
        conclusion = " ".join(str(item.get("text") or "") for item in normalized_claims)
    return {
        "status": decision.get("status", "completed"),
        "evidence_level": decision.get("evidence_level", "source-backed" if handles else "gap"),
        "evidence": evidence,
        "claims": normalized_claims,
        "events": events,
        "conclusion": conclusion,
        "unresolved": decision.get("unresolved", []),
    }


def validate_manifest_catalog(state: dict[str, Any], manifest: dict[str, Any]) -> None:
    workflow = state.get("workflow_state", {})
    if workflow.get("evidence_dirty"):
        raise ValueError("supplemental retrieval must be followed by inspect before finalize")
    catalog = workflow.get("evidence_catalog", {})
    catalog_keys = {
        (str(item.get("path")), str(item.get("section_id")))
        for item in catalog.values()
    }
    outside = [
        str(item.get("evidence_id") or "unnamed")
        for item in manifest.get("evidence", [])
        if (str(item.get("path")), str(item.get("section_id"))) not in catalog_keys
    ]
    if outside:
        raise ValueError(f"legacy manifest contains evidence not registered by inspect: {', '.join(outside)}")


def build_answer_capsule(state: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    evidence = {str(item.get("evidence_id")): item for item in manifest.get("evidence", [])}
    claims: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    source_ids: dict[str, str] = {}
    markdown: list[str] = []
    for index, claim in enumerate(manifest.get("claims", []), start=1):
        claim_text = resolve_claim_text(claim, f"capsule claim {index}")
        claim_source_ids = []
        claim_sources = []
        for evidence_id in claim.get("evidence_ids", []):
            item = evidence.get(str(evidence_id), {})
            key = str(evidence_id)
            if key not in source_ids:
                source_id = f"S{len(sources) + 1}"
                source_ids[key] = source_id
                sources.append(
                    {
                        "source_id": source_id,
                        "evidence_id": evidence_id,
                        "evidence_ref": item.get("evidence_ref"),
                        "original_pdf_filename": item.get("source_filename"),
                        "original_pdf": item.get("original_asset_path"),
                        "pages": item.get("pages", []),
                        "section_id": item.get("section_id"),
                        "viewer_url": item.get("viewer_url"),
                    }
                )
            claim_source_ids.append(source_ids[key])
            claim_sources.append(next(source for source in sources if source["source_id"] == source_ids[key]))
        claim_capsule = {
            "text": claim_text,
            "status": claim.get("status"),
            "qualification": claim.get("qualification"),
            "source_ids": claim_source_ids,
        }
        claims.append(claim_capsule)
        citation = "; ".join(
            f"{item.get('original_pdf') or 'unresolved'}, page {','.join(str(page) for page in item.get('pages', [])) or 'unresolved'}"
            for item in claim_sources
        )
        markdown.append(f"- {claim_text}" + (f" ({citation})" if citation else ""))
    return {
        "trace_id": state.get("trace_id"),
        "question_index": state.get("question_index"),
        "question": state.get("question"),
        "evidence_level": manifest.get("evidence_level"),
        "sources": sources,
        "claims": claims,
        "conclusion": manifest.get("conclusion"),
        "unresolved": manifest.get("unresolved", []),
        "answer_markdown": "\n".join(markdown),
    }


def load_manifest(args: argparse.Namespace) -> dict[str, Any]:
    provided = sum(bool(value) for value in (args.manifest, args.manifest_json, args.decision_json))
    if provided != 1:
        raise ValueError("provide exactly one of --decision-json, --manifest, or --manifest-json")
    if args.manifest:
        return load_json(args.manifest)
    value = json.loads(args.manifest_json or args.decision_json)
    if not isinstance(value, dict):
        raise ValueError("finalization input must contain one JSON object")
    return value


def expected_request_count(states: list[dict[str, Any]]) -> int | None:
    counts = {
        int(state.get("workflow_state", {}).get("expected_question_count"))
        for state in states
        if state.get("workflow_state", {}).get("expected_question_count") is not None
    }
    if len(counts) > 1:
        raise ValueError("request contains inconsistent expected question counts")
    return next(iter(counts)) if counts else None


def validate_request_completion(states: list[dict[str, Any]], request_id: str) -> None:
    if not states:
        raise ValueError(f"request has no traces: {request_id}")
    incomplete = [str(state.get("trace_id")) for state in states if state.get("status") != "completed"]
    if incomplete:
        raise ValueError(f"request contains unfinished traces: {', '.join(incomplete)}")
    missing_capsules = [str(state.get("trace_id")) for state in states if not state.get("answer_capsule")]
    if missing_capsules:
        raise ValueError(f"request traces are missing answer capsules: {', '.join(missing_capsules)}")
    indices = [int(state["question_index"]) for state in states if state.get("question_index") is not None]
    if indices and sorted(indices) != list(range(1, max(indices) + 1)):
        raise ValueError("request question indices are not contiguous from 1")
    expected = expected_request_count(states)
    if expected is not None and len(states) != expected:
        raise ValueError(f"request expected {expected} questions but contains {len(states)} traces")


def validate_close_request(vault_root: Path, state: dict[str, Any]) -> None:
    request_id = state.get("request_id")
    if not request_id:
        raise ValueError("--close-request requires a request id")
    states = grouped_states(vault_root, str(request_id))
    others = [item for item in states if item.get("trace_id") != state.get("trace_id")]
    unfinished = [str(item.get("trace_id")) for item in others if item.get("status") != "completed"]
    if unfinished:
        raise ValueError(f"cannot close request with unfinished prior traces: {', '.join(unfinished)}")
    indices = sorted(int(item["question_index"]) for item in states if item.get("question_index") is not None)
    if indices and indices != list(range(1, max(indices) + 1)):
        raise ValueError("cannot close request with non-contiguous question indices")
    if indices and state.get("question_index") != max(indices):
        raise ValueError("--close-request must be used on the highest question index")
    expected = expected_request_count(states)
    if expected is not None:
        if len(states) != expected:
            raise ValueError(f"cannot close request: expected {expected} traces but found {len(states)}")
        if state.get("question_index") != expected:
            raise ValueError("--close-request must be used on the final expected question")


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    state, _, _ = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
    if args.close_request:
        validate_close_request(vault_root, state)
    payload = load_manifest(args)
    if args.decision_json:
        manifest = decision_to_manifest(state, payload)
    else:
        manifest = payload
        validate_manifest_catalog(state, manifest)
    workflow = state.get("workflow_state", {})
    manifest.setdefault("events", [])
    manifest["events"].append(
        timed_event(
            stage="answer-synthesis",
            route="agent-synthesis",
            started_at=str(workflow.get("answer_synthesis_started_at") or state.get("updated") or command_started_at),
            started_monotonic_ns=int(workflow.get("answer_synthesis_started_monotonic_ns") or command_started_monotonic),
            started_wall_ns=int(workflow.get("answer_synthesis_started_wall_ns") or command_started_wall),
            summary="Reviewed the batched evidence packet and prepared the final claim set.",
        )
    )
    manifest["events"].append(
        timed_event(
            stage="claim-evidence-mapping",
            route="query-session-finalize",
            started_at=command_started_at,
            started_monotonic_ns=command_started_monotonic,
            started_wall_ns=command_started_wall,
            summary=f"Validated {len(manifest.get('claims', []))} claims against {len(manifest.get('evidence', []))} evidence records.",
            hit_count=len(manifest.get("claims", [])),
            evidence_ids=[str(item.get("evidence_id")) for item in manifest.get("evidence", []) if item.get("evidence_id")],
        )
    )
    session_started_monotonic = int(workflow.get("session_started_monotonic_ns") or command_started_monotonic)
    session_started_wall = int(workflow.get("session_started_wall_ns") or command_started_wall)
    manifest.setdefault("metrics", {}).update(
        {
            "query_session_duration_ms": elapsed_ms(session_started_monotonic, session_started_wall),
            "command_count": int(workflow.get("command_count") or 2) + 1,
            "measurement_boundary": "query-session begin invocation through finalize validation",
        }
    )
    capsule = build_answer_capsule(state, manifest)
    manifest["answer_capsule"] = capsule
    result = finalize_trace(vault_root, args.trace_id, manifest)
    note_path = Path(result["note_path"])
    if not note_path.is_file():
        raise FileNotFoundError(f"final trace note was not written: {note_path}")
    result.update(
        {
            "workflow": WORKFLOW,
            "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
            "query_session_duration_ms": manifest["metrics"]["query_session_duration_ms"],
            "trace_verified": True,
            "answer_capsule": capsule,
        }
    )
    if args.close_request:
        result["request"] = request_summary_payload(vault_root, str(state["request_id"]))
    return result


def request_summary_payload(vault_root: Path, request_id: str) -> dict[str, Any]:
    states = grouped_states(vault_root.resolve(), request_id)
    validate_request_completion(states, request_id)
    capsules = [state["answer_capsule"] for state in states]
    sections = []
    for index, capsule in enumerate(capsules, start=1):
        label = capsule.get("question_index") or index
        sections.append(f"## Question {label}\n\n{capsule.get('answer_markdown') or capsule.get('conclusion') or ''}")
    return {
        "workflow": WORKFLOW,
        "request_id": request_id,
        "question_count": len(capsules),
        "answer_capsules": capsules,
        "answer_markdown": "\n\n".join(sections),
        "metrics": grouped_request_metrics(states),
    }


def request_summary(args: argparse.Namespace) -> dict[str, Any]:
    return request_summary_payload(args.vault_root.resolve(), args.request_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Return exact query rules, config, session, and verification capability")
    bootstrap_parser.add_argument("vault_root", type=Path)
    bootstrap_parser.add_argument("--max-rule-chars", type=int, default=12000)
    bootstrap_parser.set_defaults(handler=bootstrap)

    begin_parser = subparsers.add_parser("begin", help="Start the trace and return compact fused scope")
    begin_parser.add_argument("vault_root", type=Path)
    begin_parser.add_argument("question")
    begin_parser.add_argument("--query-type", default="evidence")
    begin_parser.add_argument("--session-id")
    begin_parser.add_argument("--trace-id")
    begin_parser.add_argument("--request-id")
    begin_parser.add_argument("--question-index", type=int)
    begin_parser.add_argument("--question-count", type=int, help="Expected independently auditable questions in this request")
    begin_parser.add_argument("--coupled", action="store_true", help="Allow multiple subparts that require one evidence set")
    begin_parser.add_argument("--coupled-reason", help="Auditable reason that multiple subparts share one evidence set")
    begin_parser.add_argument("--provider-config", type=Path)
    begin_parser.add_argument("--top-k", type=int, default=20)
    begin_parser.add_argument("--top-documents", type=int, default=6)
    begin_parser.add_argument("--top-sections", type=int, default=12)
    begin_parser.add_argument("--compact-limit", type=int, default=5)
    begin_parser.add_argument(
        "--verification-required",
        action="store_true",
        help="Require registered-carrier visual verification; this is never inferred from question wording",
    )
    begin_parser.set_defaults(handler=begin)

    inspect_parser = subparsers.add_parser("inspect", help="Read selected source sections and evidence assets in one batch")
    inspect_parser.add_argument("vault_root", type=Path)
    inspect_parser.add_argument("trace_id")
    inspect_parser.add_argument("--candidate", action="append", default=[], help="1-based rank, section id, or document::section")
    inspect_parser.add_argument("--max-chars-per-section", type=int, default=12000)
    inspect_parser.set_defaults(handler=inspect)

    verify_parser = subparsers.add_parser("verify", help="Prepare registered visual carriers once, or return a deterministic QA downgrade")
    verify_parser.add_argument("vault_root", type=Path)
    verify_parser.add_argument("trace_id")
    verify_parser.add_argument("--evidence-ref", action="append", default=[])
    verify_parser.set_defaults(handler=prepare_verification)

    supplement_parser = subparsers.add_parser("supplement", help="Retrieve candidates for a recorded evidence gap")
    supplement_parser.add_argument("vault_root", type=Path)
    supplement_parser.add_argument("trace_id")
    supplement_parser.add_argument("query")
    supplement_parser.add_argument("--reason", required=True)
    supplement_parser.add_argument("--provider-config", type=Path)
    supplement_parser.add_argument("--top-k", type=int, default=20)
    supplement_parser.add_argument("--top-documents", type=int, default=6)
    supplement_parser.add_argument("--top-sections", type=int, default=12)
    supplement_parser.add_argument("--compact-limit", type=int, default=5)
    supplement_parser.set_defaults(handler=supplement)

    finalize_parser = subparsers.add_parser("finalize", help="Atomically record evidence, claims, events, and finish the trace")
    finalize_parser.add_argument("vault_root", type=Path)
    finalize_parser.add_argument("trace_id")
    finalize_parser.add_argument("--manifest", type=Path)
    finalize_parser.add_argument("--manifest-json")
    finalize_parser.add_argument("--decision-json")
    finalize_parser.add_argument("--close-request", action="store_true", help="Validate and return the completed request capsules")
    finalize_parser.set_defaults(handler=finalize)

    summary_parser = subparsers.add_parser("request-summary", help="Render compact capsules for a multi-question request")
    summary_parser.add_argument("vault_root", type=Path)
    summary_parser.add_argument("request_id")
    summary_parser.set_defaults(handler=request_summary)
    return parser


def record_command_failure(args: argparse.Namespace, error: Exception) -> None:
    vault_root = getattr(args, "vault_root", None)
    trace_id = getattr(args, "trace_id", None)
    if not isinstance(vault_root, Path) or not trace_id:
        return
    try:
        state, _, _ = load_state(vault_root.resolve(), str(trace_id))
        if state.get("status") != "in_progress":
            return
        workflow = state.setdefault("workflow_state", {})
        summary = (
            f"query_session {getattr(args, 'command', 'command')} failed: "
            f"{type(error).__name__}: {str(error)[:800]}. Do not modify the installed Skill during a query; "
            "finalize the trace as incomplete when the failure prevents a supported answer."
        )
        state.setdefault("events", []).append(
            clean_event(
                state,
                {
                    "stage": "query-command-failure",
                    "route": str(getattr(args, "command", "query-session")),
                    "status": "failed",
                    "summary": summary,
                    "started_at": now_iso(),
                    "ended_at": now_iso(),
                    "duration_ms": 0.0,
                },
            )
        )
        workflow["last_failure"] = summary
        workflow["recommended_next_command"] = "finalize"
        workflow["command_count"] = int(workflow.get("command_count") or 0) + 1
        write_state(vault_root.resolve(), state)
    except Exception:
        return


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
    except Exception as error:
        record_command_failure(args, error)
        raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
