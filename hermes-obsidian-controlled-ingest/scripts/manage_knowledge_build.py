#!/usr/bin/env python3
"""Run P3 UnitSet-native knowledge tasks, passes, Reduce and Build Finalize."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError, FileKnowledgeBuildService


def load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run(args):
    service = FileKnowledgeBuildService(args.vault)
    if args.command in ("plan", "pass", "reduce", "finalize"):
        method = {"plan": service.plan_task, "pass": service.record_pass,
                  "reduce": service.reduce, "finalize": service.finalize}[args.command]
        return method(load(args.request))
    if args.command in ("batch-plan", "batch-adopt", "batch-set-state", "batch-pass", "batch-reduce", "batch-finalize"):
        method = {"batch-plan": service.plan_batch, "batch-adopt": service.adopt_batch,
                  "batch-set-state": service.set_task_state_batch,
                  "batch-pass": service.record_pass_batch, "batch-reduce": service.reduce_batch,
                  "batch-finalize": service.finalize_batch}[args.command]
        return method(load(args.request))
    if args.command == "claim":
        return service.claim_task(args.task_id, args.actor, args.expected_revision)
    if args.command == "read":
        return service.reading_material(args.task_id, args.actor, args.registry_revision,
                                        args.max_codepoints)
    if args.command == "batch-prepare":
        return service.prepare_batch(args.batch_id, args.actor, args.registry_revision,
                                     args.max_codepoints)
    if args.command == "set-state":
        deferred = load(args.deferred) if args.deferred else []
        return service.set_task_state(args.task_id, args.actor, args.expected_revision,
                                      args.status, args.reason, deferred)
    if args.command == "validate":
        return service.validate_run(args.run_id)
    if args.command == "batch-validate":
        return service.validate_batch(args.batch_id)
    if args.command == "batch-status":
        return service.batch_status(args.batch_id)
    if args.command == "batch-resume":
        return service.resume_batch(args.batch_id)
    return {"ok": True, "registry": service.list_identities()}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "pass", "reduce", "finalize"):
        command = sub.add_parser(name)
        command.add_argument("--request", required=True, help="JSON request file")
    for name in ("batch-plan", "batch-adopt", "batch-set-state", "batch-pass", "batch-reduce", "batch-finalize"):
        command = sub.add_parser(name)
        command.add_argument("--request", required=True, help="JSON batch request file")
    claim = sub.add_parser("claim")
    claim.add_argument("--task-id", required=True)
    claim.add_argument("--actor", required=True)
    claim.add_argument("--expected-revision", required=True, type=int)
    reading = sub.add_parser("read")
    reading.add_argument("--task-id", required=True)
    reading.add_argument("--actor", required=True)
    reading.add_argument("--registry-revision", required=True, type=int)
    reading.add_argument("--max-codepoints", type=int)
    prepare = sub.add_parser("batch-prepare")
    prepare.add_argument("--batch-id", required=True)
    prepare.add_argument("--actor", required=True)
    prepare.add_argument("--registry-revision", required=True, type=int)
    prepare.add_argument("--max-codepoints", type=int)
    state = sub.add_parser("set-state")
    state.add_argument("--task-id", required=True)
    state.add_argument("--actor", required=True)
    state.add_argument("--expected-revision", required=True, type=int)
    state.add_argument("--status", required=True, choices=("blocked", "failed", "skipped"))
    state.add_argument("--reason", required=True)
    state.add_argument("--deferred", help="JSON array of deferred source refs/reasons")
    validate = sub.add_parser("validate")
    validate.add_argument("--run-id", required=True)
    for name in ("batch-validate", "batch-status", "batch-resume"):
        command = sub.add_parser(name)
        command.add_argument("--batch-id", required=True)
    sub.add_parser("identities")
    args = parser.parse_args()
    try:
        result = run(args)
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        detail = {"ok": False, "error": str(exc)}
        if isinstance(exc, ContractError):
            detail.update({"code": exc.code, "path": exc.path})
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
