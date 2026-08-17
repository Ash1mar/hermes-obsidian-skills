#!/usr/bin/env python3
"""Run the low-round-trip begin, inspect, and finalize controlled-query workflow."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from manage_query_trace import clean_event, finalize_trace, load_state, now_iso, start_trace, write_state
from retrieve_query_scope import compact_result, load_projections, retrieve_scope


WORKFLOW = "query-session/v1"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def vault_relative(path: Path, vault_root: Path) -> str:
    return path.resolve().relative_to(vault_root.resolve()).as_posix()


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
    preflight_finished_monotonic = time.monotonic_ns()
    started = start_trace(
        vault_root,
        args.question,
        args.session_id,
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
    packet = {
        "document_path": vault_relative(document_path, vault_root),
        "source_filename": source_filename,
        "source_path": vault_relative(source_path, vault_root) if source_path.is_file() else str(document_meta.get("source_path") or "unresolved"),
        "source_exists": source_path.is_file(),
        "source_sha256": manifest.get("source", {}).get("sha256"),
        "document_version": projection.get("source_state", {}).get("document_hash") or manifest.get("source", {}).get("sha256"),
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
        review_route = "agent-follow-up-selection"
        review_summary = "Selected additional candidates after reviewing the previous evidence packet."
        review_started_at = workflow.get("answer_synthesis_started_at")
        review_started_monotonic = workflow.get("answer_synthesis_started_monotonic_ns")
        review_started_wall = workflow.get("answer_synthesis_started_wall_ns")
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


def load_manifest(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.manifest) == bool(args.manifest_json):
        raise ValueError("provide exactly one of --manifest or --manifest-json")
    if args.manifest:
        return load_json(args.manifest)
    value = json.loads(args.manifest_json)
    if not isinstance(value, dict):
        raise ValueError("--manifest-json must contain one JSON object")
    return value


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    command_started_at = now_iso()
    command_started_monotonic = time.monotonic_ns()
    command_started_wall = time.time_ns()
    vault_root = args.vault_root.resolve()
    manifest = load_manifest(args)
    state, _, _ = load_state(vault_root, args.trace_id)
    if state.get("workflow") != WORKFLOW:
        raise ValueError(f"trace does not use {WORKFLOW}")
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
        }
    )
    return result


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

    finalize_parser = subparsers.add_parser("finalize", help="Atomically record evidence, claims, events, and finish the trace")
    finalize_parser.add_argument("vault_root", type=Path)
    finalize_parser.add_argument("trace_id")
    finalize_parser.add_argument("--manifest", type=Path)
    finalize_parser.add_argument("--manifest-json")
    finalize_parser.set_defaults(handler=finalize)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
