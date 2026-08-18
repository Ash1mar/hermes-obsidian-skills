#!/usr/bin/env python3
"""Create incremental, non-authoritative query traces for governed vault queries."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRACE_RELATIVE_DIR = Path("_system/reports/query-traces")
DATA_DIR_NAME = "_data"
SCHEMA_VERSION = "1.4"
INACTIVE_ROUTE_STATUSES = {"disabled", "unavailable", "skipped", "failed"}
RETRIEVAL_STAGES = {
    "coarse-recall",
    "hierarchical-candidate-location",
    "candidate-fusion",
    "scope-retrieval",
    "governed-artifact-lookup",
    "scoped-lexical-search",
    "supplemental-retrieval",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    if not cleaned:
        raise ValueError("trace id must contain at least one safe character")
    return cleaned[:120]


def shorten(value: str | None, limit: int = 1200) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evidence_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("evidence_id")): item
        for item in state.get("evidence", [])
        if item.get("evidence_id")
    }


def derived_event_evidence(state: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    index = evidence_by_id(state)
    return [index[item] for item in event.get("evidence_ids", []) if item in index]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def trace_paths(vault_root: Path, trace_id: str, request_id: str | None = None) -> tuple[Path, Path]:
    root = vault_root.resolve() / TRACE_RELATIVE_DIR
    safe = safe_id(trace_id)
    note_root = root / safe_id(request_id) if request_id else root
    return root / DATA_DIR_NAME / f"{safe}.query-trace.json", note_root / f"{safe}.query-trace.md"


def load_state(vault_root: Path, trace_id: str) -> tuple[dict[str, Any], Path, Path]:
    state_path, _ = trace_paths(vault_root, trace_id)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("type") != "query-trace":
        raise ValueError(f"Invalid query trace state: {state_path}")
    _, note_path = trace_paths(vault_root, trace_id, data.get("request_id"))
    return data, state_path, note_path


def yaml_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def markdown_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized:
        return ""
    label = Path(normalized).name
    return f"[[{normalized}|{label}]]"


def render_note(state: dict[str, Any]) -> str:
    events = state.get("events", [])
    route_events = [event for event in events if event.get("stage") in RETRIEVAL_STAGES]
    attempted_routes = list(dict.fromkeys(str(event.get("route")) for event in route_events if event.get("route")))
    effective_routes = list(
        dict.fromkeys(
            str(event.get("route"))
            for event in route_events
            if event.get("route") and str(event.get("status") or "").casefold() not in INACTIVE_ROUTE_STATUSES
        )
    )
    hierarchical = any(event.get("route") == "hierarchical-search" for event in events)
    metrics = state.get("metrics", {})
    session_duration = metrics.get("query_session_duration_ms")
    accounted_duration = round(
        sum(
            float(event.get("duration_ms") or 0)
            for event in events
            if event.get("accounting", "primary") == "primary"
        ),
        3,
    )
    unaccounted_duration = (
        round(max(0.0, float(session_duration) - accounted_duration), 3)
        if session_duration is not None
        else None
    )
    lines = [
        "---",
        "type: query-trace",
        f"schema_version: {yaml_string(state.get('schema_version'))}",
        "authority: non-authoritative-runtime-log",
        f"status: {yaml_string(state.get('status'))}",
        f"created: {yaml_string(state.get('created'))}",
        f"updated: {yaml_string(state.get('updated'))}",
        f"session_id: {yaml_string(state.get('session_id'))}",
        f"session_message_id: {yaml_string(state.get('session_message_id'))}",
        f"session_platform: {yaml_string(state.get('session_platform'))}",
        f"request_id: {yaml_string(state.get('request_id'))}",
        f"question_index: {yaml_string(state.get('question_index'))}",
        f"trace_id: {yaml_string(state.get('trace_id'))}",
        f"query_type: {yaml_string(state.get('query_type'))}",
        f"evidence_level: {yaml_string(state.get('evidence_level'))}",
        f"hierarchical_search_used: {'true' if hierarchical else 'false'}",
        "attempted_routes:",
    ]
    lines.extend(f"  - {yaml_string(route)}" for route in attempted_routes)
    if not attempted_routes:
        lines.append("  - none")
    lines.append("effective_routes:")
    lines.extend(f"  - {yaml_string(route)}" for route in effective_routes)
    if not effective_routes:
        lines.append("  - none")
    lines.append("retrieval_route:")
    lines.extend(f"  - {yaml_string(route)}" for route in effective_routes)
    if not effective_routes:
        lines.append("  - none")
    lines.extend(
        [
            "---",
            "",
            f"# Query Trace · {state.get('trace_id')}",
            "",
            "> [!warning] Runtime trace, not evidence",
            "> This note records retrieval navigation and decisions. It is disposable, may be incomplete, and must never be cited as source evidence.",
            "",
            "> [!question] User question",
            f"> {shorten(state.get('question'), 4000).replace(chr(10), chr(10) + '> ')}",
            "",
            "## Summary",
            "",
            f"- Status: `{state.get('status')}`",
            f"- Query type: `{state.get('query_type') or 'unclassified'}`",
            f"- Evidence level: `{state.get('evidence_level') or 'pending'}`",
            f"- Hermes session: `{state.get('session_id') or 'unavailable'}`",
            f"- Hermes message: `{state.get('session_message_id') or 'unavailable'}`",
            f"- Request group: `{state.get('request_id') or 'single query'}`",
            f"- Question index: `{state.get('question_index') or 'not grouped'}`",
            f"- Hierarchical search: `{'used' if hierarchical else 'not used'}`",
            f"- Query-session duration: `{session_duration if session_duration is not None else 'unavailable'} ms`",
            f"- Accounted stage duration: `{accounted_duration} ms`",
            f"- Unaccounted query-session duration: `{unaccounted_duration if unaccounted_duration is not None else 'unavailable'} ms`",
            "",
            "## Retrieval timeline",
            "",
        ]
    )
    if not events:
        lines.append("_No retrieval event recorded yet._")
    for index, event in enumerate(events, start=1):
        stage = event.get("stage") or "unspecified"
        status = event.get("status") or "completed"
        route = event.get("route") or "unspecified"
        lines.extend(
            [
                f"> [!info]- {index}. {stage} · {status}",
                f"> - Started: `{event.get('started_at') or 'unavailable'}`",
                f"> - Ended: `{event.get('ended_at') or event.get('at')}`",
                f"> - Route: `{route}`",
            ]
        )
        if event.get("summary"):
            lines.append(f"> - Summary: {shorten(event.get('summary')).replace(chr(10), ' ')}")
        if event.get("hit_count") is not None:
            lines.append(f"> - Hits: `{event.get('hit_count')}`")
        accepted_evidence = derived_event_evidence(state, event)
        if event.get("evidence_ids"):
            lines.append(f"> - Accepted evidence: `{len(accepted_evidence)}`")
        if event.get("duration_ms") is not None:
            lines.append(f"> - Duration: `{event.get('duration_ms')} ms`")
        for path in event.get("inspected_paths", []):
            lines.append(f"> - Inspected: {markdown_path(str(path))}")
        for evidence in accepted_evidence:
            location = ", ".join(
                value
                for value in (
                    f"section {evidence.get('section_id')}" if evidence.get("section_id") else "",
                    f"pages {evidence.get('pages')}" if evidence.get("pages") else "",
                    f"block {evidence.get('block_id')}" if evidence.get("block_id") else "",
                )
                if value
            )
            lines.append(
                f"> - Evidence `{evidence.get('evidence_id')}`: "
                f"{markdown_path(str(evidence.get('path') or ''))}"
                f"{f' — {location}' if location else ''}"
            )
        for item in event.get("rejected", []):
            lines.append(f"> - Rejected: {markdown_path(str(item.get('path', '')))} — {shorten(item.get('reason'), 400)}")
        candidates = event.get("candidates", [])
        if candidates:
            lines.append("> - Top candidates:")
            for candidate in candidates[:5]:
                title = candidate.get("title") or candidate.get("section_id") or "untitled"
                source = (
                    candidate.get("document_path")
                    or candidate.get("vault_path")
                    or candidate.get("source_filename")
                    or "unknown"
                )
                matched = candidate.get("matched_terms") or {}
                if isinstance(matched, dict):
                    terms = sorted(
                        {term for values in matched.values() if isinstance(values, list) for term in values},
                        key=lambda value: (-len(value), value),
                    )[:8]
                elif isinstance(matched, list):
                    terms = [str(term) for term in matched[:8]]
                else:
                    terms = []
                score = candidate.get("fusion_score", candidate.get("score", "?"))
                route_scores = candidate.get("route_scores") or {}
                score_detail = f"; route scores: {route_scores}" if route_scores else ""
                lines.append(
                    f">   - `{score}` {title} — {markdown_path(str(source))}; "
                    f"matched: {', '.join(terms) or 'none'}{score_detail}"
                )
        lines.append(">")
    lines.extend(["", "## Claim–Evidence map", ""])
    claims = state.get("claims", [])
    if not claims:
        lines.append("_No claim recorded yet._")
    for claim in claims:
        evidence_ids = ", ".join(f"`{item}`" for item in claim.get("evidence_ids", [])) or "none"
        lines.extend(
            [
                f"### {claim.get('claim_id')} · {claim.get('status', 'supported')}",
                "",
                shorten(str(claim.get("text") or ""), 2000),
                "",
                f"- Evidence: {evidence_ids}",
                f"- Recorded: `{claim.get('recorded_at') or 'unavailable'}`",
            ]
        )
        if claim.get("qualification"):
            lines.append(f"- Qualification: {shorten(str(claim.get('qualification')), 800)}")
        lines.append("")
    lines.extend(["## Evidence records", ""])
    evidence = state.get("evidence", [])
    if not evidence:
        lines.append("_No evidence record accepted yet._")
    for item in evidence:
        lines.extend(
            [
                f"- `{item.get('evidence_id')}` {markdown_path(str(item.get('path') or ''))}",
                f"  - Version: `{item.get('document_version') or 'unresolved'}`; section: `{item.get('section_id') or 'unresolved'}`; pages: `{item.get('pages') or 'unresolved'}`; block: `{item.get('block_id') or 'unresolved'}`",
                f"  - Original asset: `{item.get('original_asset_status') or 'not-checked'}`",
                f"  - Recorded: `{item.get('recorded_at') or 'unavailable'}`",
            ]
        )
        if item.get("original_asset_path"):
            lines.append(f"  - Original asset path: {markdown_path(str(item['original_asset_path']))}")
    lines.extend(["", "## Conclusion", "", state.get("conclusion") or "_Pending._", "", "## Unresolved / QA", ""])
    unresolved = state.get("unresolved", [])
    lines.extend(f"- {shorten(item)}" for item in unresolved)
    if not unresolved:
        lines.append("- None recorded.")
    lines.extend(
        [
            "",
            "## Runtime linkage",
            "",
            f"Use session ID `{state.get('session_id') or 'unavailable'}` and message ID `{state.get('session_message_id') or 'unavailable'}` to correlate this note with Hermes `agent.log`. Runtime logs remain the authority for tool timing and execution failures.",
            "",
        ]
    )
    return "\n".join(lines)


def request_summary_path(vault_root: Path, request_id: str) -> Path:
    return vault_root.resolve() / TRACE_RELATIVE_DIR / safe_id(request_id) / "Request Summary.md"


def grouped_states(vault_root: Path, request_id: str) -> list[dict[str, Any]]:
    data_root = vault_root.resolve() / TRACE_RELATIVE_DIR / DATA_DIR_NAME
    states = []
    for path in data_root.glob("*.query-trace.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(state, dict) and state.get("type") == "query-trace" and state.get("request_id") == request_id:
            states.append(state)
    return sorted(
        states,
        key=lambda state: (
            state.get("question_index") is None,
            state.get("question_index") or 0,
            state.get("created") or "",
            state.get("trace_id") or "",
        ),
    )


def markdown_cell(value: Any, limit: int = 240) -> str:
    return shorten(str(value or ""), limit).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_request_summary(vault_root: Path, request_id: str) -> Path:
    safe_request = safe_id(request_id)
    states = grouped_states(vault_root, safe_request)
    updated = max((str(state.get("updated") or "") for state in states), default=now_iso())
    session_ids = list(
        dict.fromkeys(str(state["session_id"]) for state in states if state.get("session_id"))
    )
    lines = [
        "---",
        "type: query-trace-request",
        f"schema_version: {yaml_string(SCHEMA_VERSION)}",
        "authority: non-authoritative-runtime-log",
        f"request_id: {yaml_string(safe_request)}",
        f"updated: {yaml_string(updated)}",
        "---",
        "",
        f"# Query Request · {safe_request}",
        "",
        "> [!warning] Runtime trace group, not evidence",
        "> This folder groups independently auditable question traces from one user request. Process order remains sequential.",
        "",
        f"- Hermes sessions: `{', '.join(session_ids) or 'unavailable'}`",
        f"- Questions recorded: `{len(states)}`",
        "",
        "| # | Status | Trace | Question |",
        "| ---: | --- | --- | --- |",
    ]
    for state in states:
        trace_id = str(state.get("trace_id") or "")
        question_index = state.get("question_index") or "-"
        note = TRACE_RELATIVE_DIR / safe_request / f"{safe_id(trace_id)}.query-trace"
        lines.append(
            f"| {question_index} | `{markdown_cell(state.get('status'), 40)}` | "
            f"[[{note.as_posix()}|{markdown_cell(trace_id, 120)}]] | "
            f"{markdown_cell(state.get('question'))} |"
        )
    path = request_summary_path(vault_root, safe_request)
    atomic_write(path, "\n".join(lines) + "\n")
    return path


def write_state(vault_root: Path, state: dict[str, Any]) -> tuple[Path, Path]:
    state["updated"] = now_iso()
    state_path, note_path = trace_paths(
        vault_root,
        str(state["trace_id"]),
        state.get("request_id"),
    )
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    atomic_write(note_path, render_note(state))
    if state.get("request_id"):
        write_request_summary(vault_root, str(state["request_id"]))
    return state_path, note_path


def ensure_dashboard(vault_root: Path) -> Path:
    path = vault_root.resolve() / TRACE_RELATIVE_DIR / "Query Trace Dashboard.md"
    if path.exists():
        return path
    content = """# Query Trace Dashboard

> [!warning] Runtime traces are navigation audit records, not evidence or governed knowledge artifacts.

```dataview
table created as Created, status as Status, query_type as Type, retrieval_route as Route, hierarchical_search_used as Hierarchical, evidence_level as Evidence
from "_system/reports/query-traces"
where type = "query-trace"
sort created desc
```
"""
    atomic_write(path, content)
    return path


def start_trace(
    vault_root: Path,
    question: str,
    session_id: str | None = None,
    query_type: str | None = None,
    trace_id: str | None = None,
    request_id: str | None = None,
    question_index: int | None = None,
    workflow: str | None = None,
) -> dict[str, Any]:
    created = now_iso()
    if question_index is not None and question_index < 1:
        raise ValueError("question index must be at least 1")
    if question_index is not None and not request_id:
        raise ValueError("question index requires request id")
    safe_request = safe_id(request_id) if request_id else None
    if safe_request:
        question_label = f"q{question_index:02d}" if question_index is not None else "q"
        generated = trace_id or f"{safe_request}_{question_label}_{uuid.uuid4().hex[:8]}"
    else:
        generated = trace_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    safe_trace = safe_id(generated)
    state_path, _ = trace_paths(vault_root, safe_trace)
    if state_path.exists():
        raise FileExistsError(f"query trace already exists: {state_path}")
    runtime_session_id = os.environ.get("HERMES_SESSION_ID") or session_id
    state = {
        "schema_version": SCHEMA_VERSION,
        "type": "query-trace",
        "authority": "non-authoritative-runtime-log",
        "trace_id": safe_trace,
        "session_id": runtime_session_id,
        "session_message_id": os.environ.get("HERMES_SESSION_MESSAGE_ID"),
        "session_platform": os.environ.get("HERMES_SESSION_PLATFORM"),
        "request_id": safe_request,
        "question_index": question_index,
        "status": "in_progress",
        "created": created,
        "updated": created,
        "question": shorten(question, 8000),
        "query_type": query_type,
        "workflow": workflow,
        "evidence_level": None,
        "events": [],
        "evidence": [],
        "claims": [],
        "open_stages": {},
        "metrics": {},
        "conclusion": None,
        "unresolved": [],
    }
    state_path, note_path = write_state(vault_root, state)
    dashboard = ensure_dashboard(vault_root)
    summary = request_summary_path(vault_root, safe_request) if safe_request else None
    return {
        "trace_id": state["trace_id"],
        "request_id": safe_request,
        "question_index": question_index,
        "state_path": str(state_path),
        "note_path": str(note_path),
        "request_summary": str(summary) if summary else None,
        "dashboard": str(dashboard),
    }


def clean_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    known_evidence = evidence_by_id(state)
    requested_evidence = [safe_id(str(item)) for item in event.get("evidence_ids", [])]
    evidence_ids = list(dict.fromkeys(requested_evidence))
    missing = [item for item in evidence_ids if item not in known_evidence]
    if missing:
        raise ValueError(f"event references unknown evidence: {', '.join(missing)}")
    ended_at = str(event.get("ended_at") or event.get("at") or now_iso())
    return {
        "at": ended_at,
        "started_at": shorten(str(event.get("started_at") or ""), 80) or None,
        "ended_at": shorten(ended_at, 80),
        "stage": shorten(str(event.get("stage") or "unspecified"), 120),
        "route": shorten(str(event.get("route") or "unspecified"), 120),
        "status": shorten(str(event.get("status") or "completed"), 40),
        "summary": shorten(str(event.get("summary") or "")),
        "hit_count": event.get("hit_count"),
        "duration_ms": event.get("duration_ms"),
        "accounting": "diagnostic" if event.get("accounting") == "diagnostic" else "primary",
        "inspected_paths": [shorten(str(item), 500) for item in event.get("inspected_paths", [])],
        "evidence_ids": evidence_ids,
        "rejected": event.get("rejected", []),
        "candidates": event.get("candidates", [])[:20],
    }


def append_events(vault_root: Path, trace_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state, _, _ = load_state(vault_root, trace_id)
    cleaned: list[dict[str, Any]] = []
    for event in events:
        item = clean_event(state, event)
        state.setdefault("events", []).append(item)
        cleaned.append(item)
    write_state(vault_root, state)
    return cleaned


def append_event(vault_root: Path, trace_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return append_events(vault_root, trace_id, [event])[0]


def parse_rejected(items: list[str]) -> list[dict[str, str]]:
    result = []
    for item in items:
        path, separator, reason = item.partition("::")
        result.append({"path": path.strip(), "reason": reason.strip() if separator else "not selected"})
    return result


def parse_pages(items: list[int]) -> list[int]:
    return sorted(set(item for item in items if item >= 1))


def validate_vault_path(vault_root: Path, value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"evidence path must be Vault-relative: {value!r}")
    resolved = (vault_root.resolve() / relative).resolve()
    resolved.relative_to(vault_root.resolve())
    if not resolved.exists():
        raise FileNotFoundError(f"evidence path does not exist: {resolved}")
    return relative.as_posix()


def normalize_evidence(vault_root: Path, state: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    evidence_id = safe_id(str(evidence.get("evidence_id") or ""))
    if evidence_id in evidence_by_id(state):
        raise ValueError(f"evidence id already exists: {evidence_id}")
    path = validate_vault_path(vault_root, str(evidence.get("path") or ""))
    asset_path = evidence.get("original_asset_path")
    if asset_path:
        asset_path = validate_vault_path(vault_root, str(asset_path))
    document_version = shorten(str(evidence.get("document_version") or ""), 300)
    if not document_version:
        raise ValueError("evidence document version must not be empty")
    original_asset_status = str(evidence.get("original_asset_status") or "not-checked")
    if original_asset_status not in {"verified", "not-required", "not-checked", "failed"}:
        raise ValueError(f"unsupported original asset status: {original_asset_status}")
    if original_asset_status == "verified" and not asset_path:
        raise ValueError("verified original assets require an original asset path")
    record = {
        "evidence_id": evidence_id,
        "path": path,
        "document_version": document_version,
        "section_id": shorten(str(evidence.get("section_id") or ""), 300) or None,
        "pages": parse_pages([int(item) for item in evidence.get("pages", [])]),
        "block_id": shorten(str(evidence.get("block_id") or ""), 300) or None,
        "original_asset_status": original_asset_status,
        "original_asset_path": asset_path,
        "summary": shorten(str(evidence.get("summary") or ""), 1200),
        "recorded_at": now_iso(),
    }
    return record


def record_evidence(vault_root: Path, trace_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    state, _, _ = load_state(vault_root, trace_id)
    record = normalize_evidence(vault_root, state, evidence)
    state.setdefault("evidence", []).append(record)
    write_state(vault_root, state)
    return record


def normalize_claim(state: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    claim_id = safe_id(str(claim.get("claim_id") or ""))
    if any(item.get("claim_id") == claim_id for item in state.get("claims", [])):
        raise ValueError(f"claim id already exists: {claim_id}")
    known = evidence_by_id(state)
    evidence_ids = list(dict.fromkeys(safe_id(str(item)) for item in claim.get("evidence_ids", [])))
    missing = [item for item in evidence_ids if item not in known]
    if missing:
        raise ValueError(f"claim references unknown evidence: {', '.join(missing)}")
    if claim.get("status") == "supported" and not evidence_ids:
        raise ValueError("supported claims require at least one evidence id")
    record = {
        "claim_id": claim_id,
        "text": shorten(str(claim.get("text") or ""), 4000),
        "status": str(claim.get("status") or "supported"),
        "evidence_ids": evidence_ids,
        "qualification": shorten(str(claim.get("qualification") or ""), 1200) or None,
        "recorded_at": now_iso(),
    }
    return record


def record_claim(vault_root: Path, trace_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    state, _, _ = load_state(vault_root, trace_id)
    record = normalize_claim(state, claim)
    state.setdefault("claims", []).append(record)
    write_state(vault_root, state)
    return record


def begin_stage(vault_root: Path, trace_id: str, stage: str, route: str) -> dict[str, Any]:
    state, _, _ = load_state(vault_root, trace_id)
    stage_id = safe_id(f"{stage}-{uuid.uuid4().hex[:8]}")
    record = {
        "stage_id": stage_id,
        "stage": shorten(stage, 120),
        "route": shorten(route, 120),
        "started_at": now_iso(),
        "started_monotonic_ns": time.monotonic_ns(),
        "started_wall_ns": time.time_ns(),
    }
    state.setdefault("open_stages", {})[stage_id] = record
    write_state(vault_root, state)
    return record


def end_stage(vault_root: Path, trace_id: str, stage_id: str, event: dict[str, Any]) -> dict[str, Any]:
    state, _, _ = load_state(vault_root, trace_id)
    timer = state.setdefault("open_stages", {}).pop(safe_id(stage_id), None)
    if not timer:
        raise ValueError(f"unknown open stage: {stage_id}")
    current_monotonic = time.monotonic_ns()
    if current_monotonic >= int(timer["started_monotonic_ns"]):
        elapsed_ns = current_monotonic - int(timer["started_monotonic_ns"])
    else:
        elapsed_ns = time.time_ns() - int(timer["started_wall_ns"])
    write_state(vault_root, state)
    return append_event(
        vault_root,
        trace_id,
        {
            **event,
            "stage": timer["stage"],
            "route": timer["route"],
            "started_at": timer["started_at"],
            "ended_at": now_iso(),
            "duration_ms": round(max(0, elapsed_ns) / 1_000_000, 3),
        },
    )


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    return start_trace(
        args.vault_root,
        args.question,
        args.session_id,
        args.query_type,
        args.trace_id,
        args.request_id,
        args.question_index,
        args.workflow,
    )


def command_event(args: argparse.Namespace) -> dict[str, Any]:
    event = append_event(
        args.vault_root,
        args.trace_id,
        {
            "stage": args.stage,
            "route": args.route,
            "status": args.status,
            "summary": args.summary,
            "hit_count": args.hit_count,
            "duration_ms": args.duration_ms,
            "inspected_paths": args.inspected_path,
            "evidence_ids": args.evidence_id,
            "rejected": parse_rejected(args.rejected),
        },
    )
    return {"trace_id": args.trace_id, "event": event}


def command_evidence(args: argparse.Namespace) -> dict[str, Any]:
    evidence = record_evidence(
        args.vault_root,
        args.trace_id,
        {
            "evidence_id": args.evidence_id,
            "path": args.path,
            "document_version": args.document_version,
            "section_id": args.section_id,
            "pages": args.page,
            "block_id": args.block_id,
            "original_asset_status": args.original_asset_status,
            "original_asset_path": args.original_asset_path,
            "summary": args.summary,
        },
    )
    return {"trace_id": args.trace_id, "evidence": evidence}


def command_claim(args: argparse.Namespace) -> dict[str, Any]:
    claim = record_claim(
        args.vault_root,
        args.trace_id,
        {
            "claim_id": args.claim_id,
            "text": args.text,
            "status": args.status,
            "evidence_ids": args.evidence_id,
            "qualification": args.qualification,
        },
    )
    return {"trace_id": args.trace_id, "claim": claim}


def command_stage_begin(args: argparse.Namespace) -> dict[str, Any]:
    stage = begin_stage(args.vault_root, args.trace_id, args.stage, args.route)
    return {"trace_id": args.trace_id, "stage": stage}


def command_stage_end(args: argparse.Namespace) -> dict[str, Any]:
    event = end_stage(
        args.vault_root,
        args.trace_id,
        args.stage_id,
        {
            "status": args.status,
            "summary": args.summary,
            "hit_count": args.hit_count,
            "inspected_paths": args.inspected_path,
            "evidence_ids": args.evidence_id,
            "rejected": parse_rejected(args.rejected),
        },
    )
    return {"trace_id": args.trace_id, "event": event}


def required_stage_gaps(state: dict[str, Any], evidence_level: str | None) -> list[str]:
    if state.get("workflow") not in {"query-session/v1", "query-session/v2"} or state.get("query_type") != "evidence":
        return []
    required = {
        "candidate-review",
        "document-reading",
        "table-figure-resolution",
        "provenance-resolution",
        "answer-synthesis",
        "claim-evidence-mapping",
    }
    if evidence_level in {"clear", "source-backed", "needs-qa"} and any(
        item.get("original_asset_status") == "verified" for item in state.get("evidence", [])
    ):
        required.add("page-asset-verification")
    observed = {str(event.get("stage")) for event in state.get("events", [])}
    return sorted(required - observed)


def finish_state(
    vault_root: Path,
    state: dict[str, Any],
    *,
    status: str,
    evidence_level: str | None,
    conclusion: str,
    unresolved: list[str],
) -> dict[str, Any]:
    if status not in {"completed", "failed", "incomplete"}:
        raise ValueError(f"unsupported trace status: {status}")
    if evidence_level not in {None, "clear", "source-backed", "needs-qa", "gap"}:
        raise ValueError(f"unsupported evidence level: {evidence_level}")
    if state.get("open_stages"):
        raise ValueError(f"cannot finish trace with open stages: {', '.join(state['open_stages'])}")
    if status == "completed" and evidence_level in {"clear", "source-backed", "needs-qa"}:
        if not state.get("evidence") or not state.get("claims"):
            raise ValueError("completed evidence-bearing traces require evidence records and claim mappings")
    gaps = required_stage_gaps(state, evidence_level) if status == "completed" else []
    if gaps:
        raise ValueError(f"completed query-session trace is missing required stages: {', '.join(gaps)}")
    state["status"] = status
    state["evidence_level"] = evidence_level
    state["conclusion"] = shorten(conclusion, 8000)
    state["unresolved"] = [shorten(item) for item in unresolved]
    state_path, note_path = write_state(vault_root, state)
    return {
        "trace_id": state["trace_id"],
        "status": state["status"],
        "state_path": str(state_path),
        "note_path": str(note_path),
        "coverage_gaps": gaps,
    }


def finalize_trace(vault_root: Path, trace_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist final evidence, claims, events, and completion in one write."""
    state, _, _ = load_state(vault_root, trace_id)
    if state.get("status") != "in_progress":
        raise ValueError(f"query trace is not in progress: {state.get('status')}")
    for evidence in manifest.get("evidence", []):
        record = normalize_evidence(vault_root, state, evidence)
        state.setdefault("evidence", []).append(record)
    for claim in manifest.get("claims", []):
        record = normalize_claim(state, claim)
        state.setdefault("claims", []).append(record)
    for event in manifest.get("events", []):
        state.setdefault("events", []).append(clean_event(state, event))
    if manifest.get("metrics"):
        state.setdefault("metrics", {}).update(manifest["metrics"])
    if manifest.get("answer_capsule"):
        state["answer_capsule"] = manifest["answer_capsule"]
    return finish_state(
        vault_root,
        state,
        status=str(manifest.get("status") or "completed"),
        evidence_level=manifest.get("evidence_level"),
        conclusion=str(manifest.get("conclusion") or ""),
        unresolved=[str(item) for item in manifest.get("unresolved", [])],
    )


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    state, _, _ = load_state(args.vault_root, args.trace_id)
    return finish_state(
        args.vault_root,
        state,
        status=args.status,
        evidence_level=args.evidence_level,
        conclusion=args.conclusion,
        unresolved=args.unresolved,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a query trace and create the Obsidian note")
    start.add_argument("vault_root", type=Path)
    start.add_argument("question")
    start.add_argument("--session-id")
    start.add_argument("--trace-id")
    start.add_argument("--request-id")
    start.add_argument("--question-index", type=int)
    start.add_argument("--query-type")
    start.add_argument("--workflow")
    start.set_defaults(handler=command_start)

    event = subparsers.add_parser("event", help="Append a retrieval or decision event")
    event.add_argument("vault_root", type=Path)
    event.add_argument("trace_id")
    event.add_argument("--stage", required=True)
    event.add_argument("--route", required=True)
    event.add_argument("--status", default="completed")
    event.add_argument("--summary", default="")
    event.add_argument("--hit-count", type=int)
    event.add_argument("--duration-ms", type=float)
    event.add_argument("--inspected-path", action="append", default=[])
    event.add_argument("--evidence-id", action="append", default=[])
    event.add_argument("--rejected", action="append", default=[], metavar="PATH::REASON")
    event.set_defaults(handler=command_event)

    evidence = subparsers.add_parser("evidence", help="Record one accepted evidence item")
    evidence.add_argument("vault_root", type=Path)
    evidence.add_argument("trace_id")
    evidence.add_argument("--evidence-id", required=True)
    evidence.add_argument("--path", required=True)
    evidence.add_argument("--document-version", required=True)
    evidence.add_argument("--section-id")
    evidence.add_argument("--page", action="append", type=int, default=[])
    evidence.add_argument("--block-id")
    evidence.add_argument(
        "--original-asset-status",
        choices=("verified", "not-required", "not-checked", "failed"),
        default="not-checked",
    )
    evidence.add_argument("--original-asset-path")
    evidence.add_argument("--summary", default="")
    evidence.set_defaults(handler=command_evidence)

    claim = subparsers.add_parser("claim", help="Map one final claim to recorded evidence")
    claim.add_argument("vault_root", type=Path)
    claim.add_argument("trace_id")
    claim.add_argument("--claim-id", required=True)
    claim.add_argument("--text", required=True)
    claim.add_argument("--status", choices=("supported", "qualified", "disputed", "gap"), default="supported")
    claim.add_argument("--evidence-id", action="append", default=[])
    claim.add_argument("--qualification")
    claim.set_defaults(handler=command_claim)

    stage_begin = subparsers.add_parser("stage-begin", help="Start a monotonic stage timer")
    stage_begin.add_argument("vault_root", type=Path)
    stage_begin.add_argument("trace_id")
    stage_begin.add_argument("--stage", required=True)
    stage_begin.add_argument("--route", required=True)
    stage_begin.set_defaults(handler=command_stage_begin)

    stage_end = subparsers.add_parser("stage-end", help="Finish a monotonic stage timer and append its event")
    stage_end.add_argument("vault_root", type=Path)
    stage_end.add_argument("trace_id")
    stage_end.add_argument("stage_id")
    stage_end.add_argument("--status", default="completed")
    stage_end.add_argument("--summary", default="")
    stage_end.add_argument("--hit-count", type=int)
    stage_end.add_argument("--inspected-path", action="append", default=[])
    stage_end.add_argument("--evidence-id", action="append", default=[])
    stage_end.add_argument("--rejected", action="append", default=[], metavar="PATH::REASON")
    stage_end.set_defaults(handler=command_stage_end)

    finish = subparsers.add_parser("finish", help="Finish or fail a query trace")
    finish.add_argument("vault_root", type=Path)
    finish.add_argument("trace_id")
    finish.add_argument("--status", choices=("completed", "failed", "incomplete"), default="completed")
    finish.add_argument("--evidence-level", choices=("clear", "source-backed", "needs-qa", "gap"))
    finish.add_argument("--conclusion", default="")
    finish.add_argument("--unresolved", action="append", default=[])
    finish.set_defaults(handler=command_finish)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
