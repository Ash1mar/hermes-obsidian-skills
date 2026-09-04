#!/usr/bin/env python3
"""Manage the stage-2 Hermes document-governance repository."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from governance_repository import (
    AUTHORITY_STATUSES,
    GOVERNANCE_STATUSES,
    ORGANIZATION_STATUSES,
    PROCESSING_STATUSES,
    GovernanceError,
    JsonGovernanceRepository,
    utc_now,
)


def source_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source_occurrence_id": args.source_occurrence_id,
        "source_organization_id": args.source_organization_id,
        "source_collection_id": args.source_collection_id,
        "batch_id": args.batch_id,
        "original_relative_path": args.original_relative_path,
        "received_at": args.received_at or utc_now(),
    }


def command_validate(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    organizations, registry = repository.load_state()
    issues = repository.validate_state(organizations, registry)
    return {
        "ok": not issues,
        "operation": "validate",
        "repository_contract": registry.get("repository_contract"),
        "backend": registry.get("backend"),
        "organization_revision": organizations.get("registry_revision"),
        "registry_revision": registry.get("registry_revision"),
        "organizations": len(organizations.get("organizations", [])),
        "document_versions": len(registry.get("records", [])),
        "issues": issues,
    }


def command_organization_add(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = repository.add_organization(
        organization_id=args.organization_id,
        name=args.name,
        aliases=args.alias or [],
        status=args.status,
        expected_revision=args.expected_revision,
        actor=args.actor,
    )
    return {"operation": "organization-add", **result}


def command_organization_status(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = repository.set_organization_status(
        organization_id=args.organization_id,
        status=args.status,
        expected_revision=args.expected_revision,
        actor=args.actor,
    )
    return {"operation": "organization-status", **result}


def command_register(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    now = args.created_at or utc_now()
    record = {
        "document_id": args.document_id,
        "version_id": args.version_id,
        "collection_id": args.collection_id,
        "title": args.title,
        "business_version": args.business_version,
        "resource_id": args.resource_id,
        "storage_uri": args.storage_uri,
        "content_sha256": args.content_sha256,
        "processing_status": args.processing_status,
        "governance_status": args.governance_status,
        "authority_status": args.authority_status,
        "supersedes_version_id": args.supersedes_version_id,
        "source_occurrences": [source_from_args(args)],
        "created_at": now,
        "updated_at": now,
    }
    return {"operation": "register", **repository.register(record, args.expected_revision, args.actor)}


def command_add_source(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = repository.add_source(
        version_id=args.version_id,
        source=source_from_args(args),
        expected_revision=args.expected_revision,
        actor=args.actor,
    )
    return {"operation": "add-source", **result}


def command_status(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    if args.processing_status is None and args.governance_status is None and args.authority_status is None:
        raise GovernanceError("status requires at least one status option")
    result = repository.set_status(
        version_id=args.version_id,
        processing_status=args.processing_status,
        governance_status=args.governance_status,
        authority_status=args.authority_status,
        expected_revision=args.expected_revision,
        actor=args.actor,
    )
    return {"operation": "status", **result}


def command_activate(repository: JsonGovernanceRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = repository.activate(
        version_id=args.version_id,
        expected_revision=args.expected_revision,
        actor=args.actor,
    )
    return {"operation": "activate", **result}


def add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-revision", type=int, required=True, help="Reject a stale writer")
    parser.add_argument("--actor", required=True, help="Human or service identity recorded in the audit event")


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print JSON output")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-occurrence-id", required=True)
    parser.add_argument("--source-organization-id", required=True)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument("--original-relative-path", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--received-at", help="ISO timestamp; defaults to current UTC time")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage hermes-governance/v1 through its JSON adapter")
    parser.add_argument("--vault", type=Path, required=True, help="Engineering Vault root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate the complete governance state")
    add_output_arguments(validate_parser)
    validate_parser.set_defaults(handler=command_validate)

    organization_add = subparsers.add_parser("organization-add", help="Add a controlled source organization")
    organization_add.add_argument("--organization-id", required=True)
    organization_add.add_argument("--name", required=True)
    organization_add.add_argument("--alias", action="append", help="Organization alias; repeatable")
    organization_add.add_argument("--status", choices=sorted(ORGANIZATION_STATUSES), default="candidate")
    add_mutation_arguments(organization_add)
    add_output_arguments(organization_add)
    organization_add.set_defaults(handler=command_organization_add)

    organization_status = subparsers.add_parser("organization-status", help="Approve or retire an organization")
    organization_status.add_argument("--organization-id", required=True)
    organization_status.add_argument("--status", choices=sorted(ORGANIZATION_STATUSES), required=True)
    add_mutation_arguments(organization_status)
    add_output_arguments(organization_status)
    organization_status.set_defaults(handler=command_organization_status)

    register_parser = subparsers.add_parser("register", help="Register one immutable document version")
    register_parser.add_argument("--document-id", required=True)
    register_parser.add_argument("--version-id", required=True)
    register_parser.add_argument("--collection-id", required=True)
    register_parser.add_argument("--title", required=True)
    register_parser.add_argument("--business-version")
    register_parser.add_argument("--resource-id", required=True)
    register_parser.add_argument("--storage-uri", required=True)
    register_parser.add_argument("--content-sha256", required=True)
    register_parser.add_argument("--processing-status", choices=sorted(PROCESSING_STATUSES), default="pending")
    register_parser.add_argument(
        "--governance-status",
        choices=sorted(GOVERNANCE_STATUSES - {"active", "superseded"}),
        default="candidate",
    )
    register_parser.add_argument("--authority-status", choices=sorted(AUTHORITY_STATUSES), default="unknown")
    register_parser.add_argument("--supersedes-version-id")
    register_parser.add_argument("--created-at", help="ISO timestamp; defaults to current UTC time")
    add_source_arguments(register_parser)
    add_mutation_arguments(register_parser)
    add_output_arguments(register_parser)
    register_parser.set_defaults(handler=command_register)

    add_source_parser = subparsers.add_parser("add-source", help="Append a source occurrence to a version")
    add_source_parser.add_argument("--version-id", required=True)
    add_source_arguments(add_source_parser)
    add_mutation_arguments(add_source_parser)
    add_output_arguments(add_source_parser)
    add_source_parser.set_defaults(handler=command_add_source)

    status_parser = subparsers.add_parser("status", help="Change non-activation document statuses")
    status_parser.add_argument("--version-id", required=True)
    status_parser.add_argument("--processing-status", choices=sorted(PROCESSING_STATUSES))
    status_parser.add_argument(
        "--governance-status",
        choices=sorted(GOVERNANCE_STATUSES - {"active", "superseded"}),
    )
    status_parser.add_argument("--authority-status", choices=sorted(AUTHORITY_STATUSES))
    add_mutation_arguments(status_parser)
    add_output_arguments(status_parser)
    status_parser.set_defaults(handler=command_status)

    activate_parser = subparsers.add_parser("activate", help="Atomically activate a version and supersede the old one")
    activate_parser.add_argument("--version-id", required=True)
    add_mutation_arguments(activate_parser)
    add_output_arguments(activate_parser)
    activate_parser.set_defaults(handler=command_activate)
    return parser


def print_result(result: dict[str, Any], as_json: bool) -> None:
    envelope = {
        "tool": "hermes-document-governance",
        "schema_version": "1.0",
        **result,
    }
    if as_json:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return
    for key, value in envelope.items():
        if key != "issues":
            print(f"{key}: {value}")
    for item in envelope.get("issues", []):
        print(f"- [{item['code']}] {item['path']}: {item['message']}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        repository = JsonGovernanceRepository(args.vault)
        result = args.handler(repository, args)
    except (GovernanceError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print_result(result, args.json)
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
