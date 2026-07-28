#!/usr/bin/env python3
"""Create incremental, non-authoritative query traces for governed vault queries."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRACE_RELATIVE_DIR = Path("_system/reports/query-traces")
DATA_DIR_NAME = "_data"
SCHEMA_VERSION = "1.1"


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
    routes = list(dict.fromkeys(str(event.get("route")) for event in events if event.get("route")))
    hierarchical = any(event.get("route") == "hierarchical-search" for event in events)
    lines = [
        "---",
        "type: query-trace",
        f"schema_version: {yaml_string(state.get('schema_version'))}",
        "authority: non-authoritative-runtime-log",
        f"status: {yaml_string(state.get('status'))}",
        f"created: {yaml_string(state.get('created'))}",
        f"updated: {yaml_string(state.get('updated'))}",
        f"session_id: {yaml_string(state.get('session_id'))}",
        f"request_id: {yaml_string(state.get('request_id'))}",
        f"question_index: {yaml_string(state.get('question_index'))}",
        f"trace_id: {yaml_string(state.get('trace_id'))}",
        f"query_type: {yaml_string(state.get('query_type'))}",
        f"evidence_level: {yaml_string(state.get('evidence_level'))}",
        f"hierarchical_search_used: {'true' if hierarchical else 'false'}",
        "retrieval_route:",
    ]
    lines.extend(f"  - {yaml_string(route)}" for route in routes)
    if not routes:
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
            f"- Request group: `{state.get('request_id') or 'single query'}`",
            f"- Question index: `{state.get('question_index') or 'not grouped'}`",
            f"- Hierarchical search: `{'used' if hierarchical else 'not used'}`",
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
                f"> - At: `{event.get('at')}`",
                f"> - Route: `{route}`",
            ]
        )
        if event.get("summary"):
            lines.append(f"> - Summary: {shorten(event.get('summary')).replace(chr(10), ' ')}")
        if event.get("hit_count") is not None:
            lines.append(f"> - Hits: `{event.get('hit_count')}`")
        if event.get("accepted_count") is not None:
            lines.append(f"> - Accepted: `{event.get('accepted_count')}`")
        if event.get("duration_ms") is not None:
            lines.append(f"> - Duration: `{event.get('duration_ms')} ms`")
        for path in event.get("inspected_paths", []):
            lines.append(f"> - Inspected: {markdown_path(str(path))}")
        for path in event.get("accepted_paths", []):
            lines.append(f"> - Accepted: {markdown_path(str(path))}")
        for item in event.get("rejected", []):
            lines.append(f"> - Rejected: {markdown_path(str(item.get('path', '')))} — {shorten(item.get('reason'), 400)}")
        candidates = event.get("candidates", [])
        if candidates:
            lines.append("> - Top candidates:")
            for candidate in candidates[:10]:
                title = candidate.get("title") or candidate.get("section_id") or "untitled"
                source = candidate.get("document_path") or candidate.get("source_filename") or "unknown"
                matched = candidate.get("matched_terms") or {}
                terms = sorted({term for values in matched.values() if isinstance(values, list) for term in values})
                lines.append(
                    f">   - `{candidate.get('score', '?')}` {title} — {markdown_path(str(source))}; matched: {', '.join(terms) or 'none'}"
                )
        lines.append(">")
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
            f"Use session ID `{state.get('session_id') or 'unavailable'}` to correlate this note with Hermes `agent.log`. Runtime logs remain the authority for tool timing and execution failures.",
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
    state = {
        "schema_version": SCHEMA_VERSION,
        "type": "query-trace",
        "authority": "non-authoritative-runtime-log",
        "trace_id": safe_trace,
        "session_id": session_id,
        "request_id": safe_request,
        "question_index": question_index,
        "status": "in_progress",
        "created": created,
        "updated": created,
        "question": shorten(question, 8000),
        "query_type": query_type,
        "evidence_level": None,
        "events": [],
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


def append_event(vault_root: Path, trace_id: str, event: dict[str, Any]) -> dict[str, Any]:
    state, _, _ = load_state(vault_root, trace_id)
    clean_event = {
        "at": now_iso(),
        "stage": shorten(str(event.get("stage") or "unspecified"), 120),
        "route": shorten(str(event.get("route") or "unspecified"), 120),
        "status": shorten(str(event.get("status") or "completed"), 40),
        "summary": shorten(str(event.get("summary") or "")),
        "hit_count": event.get("hit_count"),
        "accepted_count": event.get("accepted_count"),
        "duration_ms": event.get("duration_ms"),
        "inspected_paths": [shorten(str(item), 500) for item in event.get("inspected_paths", [])],
        "accepted_paths": [shorten(str(item), 500) for item in event.get("accepted_paths", [])],
        "rejected": event.get("rejected", []),
        "candidates": event.get("candidates", [])[:20],
    }
    state.setdefault("events", []).append(clean_event)
    write_state(vault_root, state)
    return clean_event


def parse_rejected(items: list[str]) -> list[dict[str, str]]:
    result = []
    for item in items:
        path, separator, reason = item.partition("::")
        result.append({"path": path.strip(), "reason": reason.strip() if separator else "not selected"})
    return result


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    return start_trace(
        args.vault_root,
        args.question,
        args.session_id,
        args.query_type,
        args.trace_id,
        args.request_id,
        args.question_index,
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
            "accepted_count": args.accepted_count,
            "duration_ms": args.duration_ms,
            "inspected_paths": args.inspected_path,
            "accepted_paths": args.accepted_path,
            "rejected": parse_rejected(args.rejected),
        },
    )
    return {"trace_id": args.trace_id, "event": event}


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    state, _, _ = load_state(args.vault_root, args.trace_id)
    state["status"] = args.status
    state["evidence_level"] = args.evidence_level
    state["conclusion"] = shorten(args.conclusion, 8000)
    state["unresolved"] = [shorten(item) for item in args.unresolved]
    state_path, note_path = write_state(args.vault_root, state)
    return {"trace_id": args.trace_id, "status": state["status"], "state_path": str(state_path), "note_path": str(note_path)}


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
    start.set_defaults(handler=command_start)

    event = subparsers.add_parser("event", help="Append a retrieval or decision event")
    event.add_argument("vault_root", type=Path)
    event.add_argument("trace_id")
    event.add_argument("--stage", required=True)
    event.add_argument("--route", required=True)
    event.add_argument("--status", default="completed")
    event.add_argument("--summary", default="")
    event.add_argument("--hit-count", type=int)
    event.add_argument("--accepted-count", type=int)
    event.add_argument("--duration-ms", type=float)
    event.add_argument("--inspected-path", action="append", default=[])
    event.add_argument("--accepted-path", action="append", default=[])
    event.add_argument("--rejected", action="append", default=[], metavar="PATH::REASON")
    event.set_defaults(handler=command_event)

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
