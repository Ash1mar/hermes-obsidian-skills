#!/usr/bin/env python3
"""Project Vault ingest workflow nodes onto Hermes Kanban (phase-6 adapter)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError
from ingest_kanban import IngestKanbanAdapter


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "sync", "cancel", "resume", "worker-begin", "worker-check",
                 "worker-register-source", "worker-heartbeat", "worker-complete", "worker-fail", "arm-canary",
                 "disarm-canary"):
        sub.add_parser(name).add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
        if args.command.startswith("worker-"):
            sibling = Path(__file__).resolve().parents[2] / "hermes-obsidian-governed-ingest-orchestrator/lib"
            if not (sibling / "orchestration.py").is_file():
                raise ContractError("WORKER_CONTRACT_UNAVAILABLE", "$", "install the governed-ingest orchestrator Skill")
            sys.path.insert(0, str(sibling))
            from orchestration import dispatch
            result = dispatch(args.vault, args.command, request)
        else:
            adapter = IngestKanbanAdapter(args.vault)
            result = getattr(adapter, args.command.replace("-", "_"))(request)
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        detail = {"ok": False, "error": str(exc)}
        if isinstance(exc, ContractError):
            detail.update({"code": exc.code, "path": exc.path})
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
