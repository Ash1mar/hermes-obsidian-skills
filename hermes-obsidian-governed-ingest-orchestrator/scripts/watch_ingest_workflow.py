#!/usr/bin/env python3
"""Reconcile worker Vault outcomes from a trusted, non-worker process."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _vault_path
from ingest_kanban import IngestKanbanAdapter


def watch(vault: str, workflow_id: str, interval: float = 3) -> None:
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") or os.environ.get("HERMES_KANBAN_TASK"):
        raise RuntimeError("reconciler must run outside a Hermes worker or delegated child")
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    lock = _vault_path(adapter.workflow.vault,
        f"_system/ledgers/ingest-workflows/.watch-locks/{workflow_id}.lock")
    with _exclusive_lock(lock):
        while True:
            value = adapter.workflow.status(workflow_id)
            if value["cancel_requested"] or value["state"] in ("completed", "partial", "failed"):
                return
            try:
                result = adapter._sync_current(workflow_id)
                if result.get("canary_complete") and value["scope"].get("execution_mode") == "canary_only":
                    return
            except ContractError as exc:
                if exc.code not in ("KANBAN_UNAVAILABLE", "REVISION_CONFLICT"):
                    raise
            time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    try:
        watch(args.vault, args.workflow_id, args.interval)
    except (ContractError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
