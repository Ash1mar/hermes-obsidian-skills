#!/usr/bin/env python3
"""Plan, apply and validate a P4 Vault Finalize release."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError, FileVaultFinalizeService


def request(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--release-id", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        service = FileVaultFinalizeService(args.vault)
        if args.command == "plan":
            result = service.plan(request(args.request))
        elif args.command == "apply":
            result = service.apply(request(args.request))
        elif args.command == "validate":
            result = service.validate(args.release_id)
        else:
            result = service.status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"ok": False, "error": getattr(exc, "code", type(exc).__name__),
                   "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
