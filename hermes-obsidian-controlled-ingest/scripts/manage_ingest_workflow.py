#!/usr/bin/env python3
"""Manage the Vault-authoritative ingest workflow ledger (no dispatch)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import (ContractError, FileIngestWorkflowService,
                                 mutation_digest)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "resume", "approve", "cancel", "reconcile", "rebuild-kanban", "bind-kanban", "pin-templates", "digest"):
        sub.add_parser(name).add_argument("--request", required=True)
    status = sub.add_parser("status")
    status.add_argument("--workflow-id", required=True)
    status.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = FileIngestWorkflowService(args.vault).status(args.workflow_id, args.compact)
        else:
            request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
            if args.command == "digest":
                result = {"input_digest": mutation_digest(request)}
            else:
                service = FileIngestWorkflowService(args.vault)
                result = getattr(service, args.command.replace("-", "_"))(request)
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
