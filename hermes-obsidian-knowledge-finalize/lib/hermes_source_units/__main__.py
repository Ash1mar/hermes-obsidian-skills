"""Read-only P0 contract validator: python3 -m hermes_source_units KIND FILE."""

import argparse
import json
from pathlib import Path

from .validation import ContractError, validate_record, validate_references


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind")
    parser.add_argument("record", type=Path)
    parser.add_argument("--units", type=Path, help="JSON array of source-unit records for exact-reference checks")
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8-sig"))
        if args.units:
            units = json.loads(args.units.read_text(encoding="utf-8-sig"))
            if not isinstance(units, list):
                raise ContractError("INVALID_SCHEMA", "$", "units file must contain an array")
            validate_references(args.kind, record, units)
        else:
            validate_record(args.kind, record)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "INVALID_INPUT"), "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "kind": args.kind, "level": "references" if args.units else "record", "source_integrity_verified": False, "authorization_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
