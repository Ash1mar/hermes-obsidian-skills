#!/usr/bin/env python3
"""Prepare, build, inspect and validate P2 source units."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError, FileSourceUnitService, UnicodeCodepointCounter


def load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(args):
    service = FileSourceUnitService(args.vault)
    if args.command == "prepare-markdown":
        return service.prepare_markdown(args.source, args.document_id, args.version_id, args.resource_id)
    if args.command == "prepare-bundle":
        return service.prepare_bundle(args.bundle)
    if args.command in ("preview", "build"):
        request = {"artifact_manifest": args.artifact_manifest, "config": service.config,
                   "actor": args.actor, "expected_revision": args.expected_revision}
        return getattr(service, args.command)(request)
    if args.command == "list":
        return {"units": service.list(args.resource_id, args.unit_set_id)}
    if args.command == "validate":
        return service.validate(args.resource_id, args.unit_set_id)
    if args.command == "audit-tokens":
        return service.audit_tokens(args.resource_id, UnicodeCodepointCounter(),
                                    args.unit_set_id, args.max_tokens)
    access = {"actor": args.actor, "purpose": args.purpose,
              "registry_revision": args.registry_revision}
    if args.command == "get":
        return service.get({"source_ref": load(args.source_ref), "access": access})
    return service.context({"core_refs": load(args.core_refs), "access": access,
                            "max_codepoints": args.max_codepoints})


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    markdown = sub.add_parser("prepare-markdown")
    markdown.add_argument("--source", required=True, help="Vault-relative immutable Markdown source")
    for name in ("document-id", "version-id", "resource-id"):
        markdown.add_argument(f"--{name}", required=True)
    bundle = sub.add_parser("prepare-bundle")
    bundle.add_argument("--bundle", required=True, help="Vault-relative governed Bundle v2 directory")
    for name in ("preview", "build"):
        command = sub.add_parser(name)
        command.add_argument("--artifact-manifest", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--expected-revision", required=True, type=int)
    listing = sub.add_parser("list")
    listing.add_argument("--resource-id", required=True)
    listing.add_argument("--unit-set-id")
    validate = sub.add_parser("validate")
    validate.add_argument("--resource-id", required=True)
    validate.add_argument("--unit-set-id")
    audit = sub.add_parser("audit-tokens")
    audit.add_argument("--resource-id", required=True)
    audit.add_argument("--unit-set-id")
    audit.add_argument("--max-tokens", type=int)
    for name, field in (("get", "source-ref"), ("context", "core-refs")):
        command = sub.add_parser(name)
        command.add_argument(f"--{field}", required=True, help="JSON file")
        command.add_argument("--actor", required=True)
        command.add_argument("--purpose", choices=("construction", "query", "qa"), required=True)
        command.add_argument("--registry-revision", type=int, required=True)
        if name == "context":
            command.add_argument("--max-codepoints", type=int, required=True)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
