#!/usr/bin/env python3
"""Run the low-round-trip begin, inspect, and finalize controlled-query workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
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


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def vault_relative(path: Path, vault_root: Path) -> str:
    return path.resolve().relative_to(vault_root.resolve()).as_posix()


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


def select_candidates(candidates: list[dict[str, Any]], selectors: list[str]) -> list[dict[str, Any]]:
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
        if not handle:
            handle = f"P{len(catalog) + 1}"
            catalog[handle] = {
                "handle": handle,
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
                "inspection_rounds": [],
            }
            existing[key] = handle
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
) -> Path:
    candidates: list[Path] = []
    for value in (document_meta.get("source_path"), manifest.get("source", {}).get("path")):
        if not value:
            continue
        path = Path(str(value))
        candidates.append(path if path.is_absolute() else vault_root / path)
    candidates.append(vault_root / "10_Raw" / source_filename)
    return next((path for path in candidates if path.is_file()), candidates[-1])


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
        "source_path": vault_relative(source_path, vault_root) if source_path.is_file() else str(document_meta.get("source_path") or "unresolved"),
        "source_exists": source_path.is_file(),
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
        },
        "viewer_url": viewer_url,
    }
    return packet, timings, inspected


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
    selected = select_candidates(fused_candidates(state), args.candidate)
    projections = load_projections(vault_root)
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
    return {
        "workflow": WORKFLOW,
        "trace_id": args.trace_id,
        "selected_count": len(packets),
        "duration_ms": elapsed_ms(command_started_monotonic, command_started_wall),
        "evidence_packets": packets,
        "next_command": "finalize",
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


def referenced_packet_handles(claims: list[dict[str, Any]]) -> list[str]:
    handles: list[str] = []
    for claim in claims:
        for value in claim.get("evidence_refs", []):
            handle = str(value)
            if handle not in handles:
                handles.append(handle)
    return handles


def decision_to_manifest(state: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    workflow = state.get("workflow_state", {})
    if workflow.get("evidence_dirty"):
        raise ValueError("supplemental retrieval must be followed by inspect before finalize")
    catalog = workflow.get("evidence_catalog", {})
    claims = decision.get("claims", [])
    if not isinstance(claims, list):
        raise ValueError("decision claims must be a list")
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
    markdown: list[str] = []
    for index, claim in enumerate(manifest.get("claims", []), start=1):
        claim_text = resolve_claim_text(claim, f"capsule claim {index}")
        sources = []
        for evidence_id in claim.get("evidence_ids", []):
            item = evidence.get(str(evidence_id), {})
            sources.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_ref": item.get("evidence_ref"),
                    "original_pdf_filename": item.get("source_filename"),
                    "original_pdf": item.get("original_asset_path"),
                    "pages": item.get("pages", []),
                    "section_id": item.get("section_id"),
                    "viewer_url": item.get("viewer_url"),
                }
            )
        claim_capsule = {
            "text": claim_text,
            "status": claim.get("status"),
            "qualification": claim.get("qualification"),
            "sources": sources,
        }
        claims.append(claim_capsule)
        citation = "; ".join(
            f"{item.get('original_pdf') or 'unresolved'}, page {','.join(str(page) for page in item.get('pages', [])) or 'unresolved'}"
            for item in sources
        )
        markdown.append(f"- {claim_text}" + (f" ({citation})" if citation else ""))
    return {
        "trace_id": state.get("trace_id"),
        "question_index": state.get("question_index"),
        "question": state.get("question"),
        "evidence_level": manifest.get("evidence_level"),
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


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    state, _, _ = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
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
    return result


def request_summary(args: argparse.Namespace) -> dict[str, Any]:
    states = grouped_states(args.vault_root.resolve(), args.request_id)
    capsules = [state.get("answer_capsule") for state in states if state.get("answer_capsule")]
    if not capsules:
        raise ValueError(f"request has no finalized answer capsules: {args.request_id}")
    sections = []
    for index, capsule in enumerate(capsules, start=1):
        label = capsule.get("question_index") or index
        sections.append(f"## Question {label}\n\n{capsule.get('answer_markdown') or capsule.get('conclusion') or ''}")
    return {
        "workflow": WORKFLOW,
        "request_id": args.request_id,
        "question_count": len(capsules),
        "answer_capsules": capsules,
        "answer_markdown": "\n\n".join(sections),
        "metrics": grouped_request_metrics(states),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin_parser = subparsers.add_parser("begin", help="Start the trace and return compact fused scope")
    begin_parser.add_argument("vault_root", type=Path)
    begin_parser.add_argument("question")
    begin_parser.add_argument("--query-type", default="evidence")
    begin_parser.add_argument("--session-id")
    begin_parser.add_argument("--trace-id")
    begin_parser.add_argument("--request-id")
    begin_parser.add_argument("--question-index", type=int)
    begin_parser.add_argument("--coupled", action="store_true", help="Allow multiple subparts that require one evidence set")
    begin_parser.add_argument("--coupled-reason", help="Auditable reason that multiple subparts share one evidence set")
    begin_parser.add_argument("--provider-config", type=Path)
    begin_parser.add_argument("--top-k", type=int, default=20)
    begin_parser.add_argument("--top-documents", type=int, default=6)
    begin_parser.add_argument("--top-sections", type=int, default=12)
    begin_parser.add_argument("--compact-limit", type=int, default=5)
    begin_parser.set_defaults(handler=begin)

    inspect_parser = subparsers.add_parser("inspect", help="Read selected source sections and evidence assets in one batch")
    inspect_parser.add_argument("vault_root", type=Path)
    inspect_parser.add_argument("trace_id")
    inspect_parser.add_argument("--candidate", action="append", default=[], help="1-based rank, section id, or document::section")
    inspect_parser.add_argument("--max-chars-per-section", type=int, default=16000)
    inspect_parser.set_defaults(handler=inspect)

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
    finalize_parser.set_defaults(handler=finalize)

    summary_parser = subparsers.add_parser("request-summary", help="Render compact capsules for a multi-question request")
    summary_parser.add_argument("vault_root", type=Path)
    summary_parser.add_argument("request_id")
    summary_parser.set_defaults(handler=request_summary)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
