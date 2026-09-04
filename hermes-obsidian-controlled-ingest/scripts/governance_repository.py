#!/usr/bin/env python3
"""Storage-neutral governance operations with a stage-2 JSON repository."""

from __future__ import annotations

import copy
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from uuid import uuid4


SCHEMA_VERSION = "1.0"
REPOSITORY_CONTRACT = "hermes-governance/v1"
STABLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROCESSING_STATUSES = {"pending", "processing", "completed", "failed"}
GOVERNANCE_STATUSES = {"candidate", "active", "superseded", "withdrawn", "unknown"}
AUTHORITY_STATUSES = {"official", "reference", "draft", "unofficial", "unknown"}
ORGANIZATION_STATUSES = {"candidate", "approved", "retired"}
ALLOWED_STORAGE_SCHEMES = {"local", "oss", "s3"}


class GovernanceError(RuntimeError):
    """Raised when a governance operation cannot be completed safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_stable_id(value: Any) -> bool:
    return isinstance(value, str) and STABLE_ID_PATTERN.fullmatch(value) is not None


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value and parsed.tzinfo is not None


def valid_storage_uri(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in ALLOWED_STORAGE_SCHEMES or not (parsed.netloc or parsed.path):
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    return True


def safe_control_path(vault: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"Missing governance control path: {label}")
    pure = PurePosixPath(value.strip().replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or ":" in pure.parts[0]:
        raise GovernanceError(f"Governance control path must remain inside the Vault: {label}")
    resolved = vault.joinpath(*pure.parts).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise GovernanceError(f"Governance control path escapes the Vault: {label}") from exc
    return resolved


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"Cannot read governance JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"Governance JSON root must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def issue(code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message, "details": details}


class JsonGovernanceRepository:
    """The JSON adapter for ``hermes-governance/v1``."""

    def __init__(self, vault: Path):
        self.vault = vault.expanduser().resolve()
        self.manifest_path = self.vault / "_system" / "vault.json"
        self.manifest = load_json(self.manifest_path)
        governance = self.manifest.get("governance")
        if not isinstance(governance, dict):
            raise GovernanceError("_system/vault.json must contain a governance object")
        repository = governance.get("repository")
        if not isinstance(repository, dict):
            raise GovernanceError("_system/vault.json must declare governance.repository")
        if repository.get("contract") != REPOSITORY_CONTRACT:
            raise GovernanceError("Unsupported governance repository contract")
        if repository.get("backend") != "json":
            raise GovernanceError("This stage-2 command supports only repository.backend=json")
        self.registry_path = safe_control_path(
            self.vault, repository.get("registry_path"), "governance.repository.registry_path"
        )
        self.organizations_path = safe_control_path(
            self.vault, governance.get("organizations_path"), "governance.organizations_path"
        )
        self.schema_path = safe_control_path(
            self.vault, governance.get("schema_path"), "governance.schema_path"
        )
        self.lock_path = self.vault / "_system" / "metadata" / ".document-governance.lock"

    @contextmanager
    def mutation_lock(self, actor: str) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise GovernanceError(
                f"Governance mutation lock already exists: {self.lock_path}. "
                "Confirm no writer is running before removing a stale lock."
            ) from exc
        try:
            os.write(descriptor, json.dumps({"actor": actor, "created_at": utc_now()}).encode("utf-8"))
            os.close(descriptor)
            descriptor = -1
            yield
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def load_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return load_json(self.organizations_path), load_json(self.registry_path)

    def validate(self) -> list[dict[str, Any]]:
        organizations, registry = self.load_state()
        return self.validate_state(organizations, registry)

    def validate_state(
        self,
        organizations: dict[str, Any],
        registry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            issues.append(issue("schema_mismatch", "_system/vault.json", "Unsupported Vault schema version."))
        vault_config = self.manifest.get("vault")
        governance = self.manifest.get("governance")
        if not isinstance(vault_config, dict) or vault_config.get("profile") != "engineering":
            issues.append(issue("profile_mismatch", "_system/vault.json", "Governance requires engineering profile."))
        elif not valid_stable_id(vault_config.get("id")) or not valid_stable_id(vault_config.get("security_domain")):
            issues.append(issue("invalid_id", "_system/vault.json", "Vault id and security_domain must be stable IDs."))
        if not isinstance(governance, dict) or governance.get("enabled") is not True:
            issues.append(issue("invalid_manifest", "_system/vault.json", "Governance must be enabled."))
        if not self.schema_path.is_file():
            issues.append(issue("missing_schema", str(self.schema_path), "Governance schema file is missing."))
        else:
            schema = load_json(self.schema_path)
            mapping = schema.get("x-hermes-database-mapping")
            planned_backends = mapping.get("planned_backends") if isinstance(mapping, dict) else None
            if (
                schema.get("$id") != "urn:hermes:document-governance:1.0"
                or not isinstance(planned_backends, list)
                or not {"sqlite", "postgresql"}.issubset(set(planned_backends))
            ):
                issues.append(issue("schema_mismatch", str(self.schema_path), "Governance schema or SQL mapping is unsupported."))

        organization_by_id: dict[str, dict[str, Any]] = {}
        labels: dict[str, str] = {}
        if organizations.get("schema_version") != SCHEMA_VERSION:
            issues.append(issue("schema_mismatch", str(self.organizations_path), "Unsupported organization schema."))
        organization_revision = organizations.get("registry_revision")
        if not isinstance(organization_revision, int) or isinstance(organization_revision, bool) or organization_revision < 0:
            issues.append(issue("invalid_revision", str(self.organizations_path), "Organization revision must be non-negative."))
        organization_items = organizations.get("organizations")
        if not isinstance(organization_items, list):
            issues.append(issue("invalid_shape", str(self.organizations_path), "organizations must be a list."))
            organization_items = []
        for index, organization in enumerate(organization_items):
            item_path = f"source-organizations.json#organizations[{index}]"
            if not isinstance(organization, dict):
                issues.append(issue("invalid_shape", item_path, "Organization must be an object."))
                continue
            organization_id = organization.get("id")
            if not valid_stable_id(organization_id):
                issues.append(issue("invalid_id", item_path, "Organization id is invalid."))
                continue
            if organization_id in organization_by_id:
                issues.append(issue("duplicate_id", item_path, "Organization id is duplicated."))
            organization_by_id[organization_id] = organization
            if not isinstance(organization.get("name"), str) or not organization["name"].strip():
                issues.append(issue("missing_field", item_path, "Organization name is required."))
            if organization.get("status") not in ORGANIZATION_STATUSES:
                issues.append(issue("invalid_status", item_path, "Organization status is invalid."))
            aliases = organization.get("aliases")
            if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
                issues.append(issue("invalid_shape", item_path, "Organization aliases must be a string list."))
                aliases = []
            for label in [organization.get("name"), *aliases]:
                if not isinstance(label, str) or not label.strip():
                    continue
                normalized = label.strip().casefold()
                owner = labels.get(normalized)
                if owner is not None and owner != organization_id:
                    issues.append(issue("organization_alias_conflict", item_path, "Organization label is ambiguous."))
                labels[normalized] = organization_id

        organization_events = organizations.get("events")
        if not isinstance(organization_events, list):
            issues.append(issue("invalid_shape", str(self.organizations_path), "events must be a list."))
            organization_events = []
        issues.extend(self._validate_events(organization_events, organization_revision, "source-organizations.json"))

        if registry.get("schema_version") != SCHEMA_VERSION or registry.get("repository_contract") != REPOSITORY_CONTRACT:
            issues.append(issue("schema_mismatch", str(self.registry_path), "Unsupported registry schema or contract."))
        if registry.get("backend") != "json":
            issues.append(issue("backend_unsupported", str(self.registry_path), "Registry backend must be json."))
        registry_revision = registry.get("registry_revision")
        if not isinstance(registry_revision, int) or isinstance(registry_revision, bool) or registry_revision < 0:
            issues.append(issue("invalid_revision", str(self.registry_path), "Registry revision must be non-negative."))
        records = registry.get("records")
        if not isinstance(records, list):
            issues.append(issue("invalid_shape", str(self.registry_path), "records must be a list."))
            records = []
        registry_events = registry.get("events")
        if not isinstance(registry_events, list):
            issues.append(issue("invalid_shape", str(self.registry_path), "events must be a list."))
            registry_events = []
        issues.extend(self._validate_events(registry_events, registry_revision, "document-registry.json"))

        versions: dict[str, dict[str, Any]] = {}
        resources: dict[str, str] = {}
        content_hashes: dict[str, str] = {}
        occurrences: set[str] = set()
        active_documents: dict[str, str] = {}
        required = {
            "document_id", "version_id", "collection_id", "title", "resource_id", "storage_uri",
            "content_sha256", "processing_status", "governance_status", "authority_status",
            "source_occurrences", "created_at", "updated_at",
        }
        for index, record in enumerate(records):
            item_path = f"document-registry.json#records[{index}]"
            if not isinstance(record, dict):
                issues.append(issue("invalid_shape", item_path, "Document version must be an object."))
                continue
            missing = sorted(field for field in required if record.get(field) in (None, ""))
            if missing:
                issues.append(issue("missing_field", item_path, "Document version has missing fields.", missing=missing))
            for field in ("document_id", "version_id", "collection_id", "resource_id"):
                if not valid_stable_id(record.get(field)):
                    issues.append(issue("invalid_id", item_path, f"{field} is invalid."))
            version_id = record.get("version_id")
            if isinstance(version_id, str):
                if version_id in versions:
                    issues.append(issue("duplicate_id", item_path, "version_id is duplicated."))
                versions[version_id] = record
            resource_id = record.get("resource_id")
            if isinstance(resource_id, str):
                prior_version = resources.get(resource_id)
                if prior_version is not None and prior_version != version_id:
                    issues.append(issue("duplicate_resource", item_path, "resource_id belongs to multiple versions."))
                resources[resource_id] = str(version_id)
            if not isinstance(record.get("title"), str) or not record["title"].strip():
                issues.append(issue("missing_field", item_path, "Document title is required."))
            content_hash = str(record.get("content_sha256", ""))
            if not SHA256_PATTERN.fullmatch(content_hash):
                issues.append(issue("invalid_hash", item_path, "content_sha256 is invalid."))
            else:
                prior_hash_version = content_hashes.get(content_hash)
                if prior_hash_version is not None and prior_hash_version != version_id:
                    issues.append(issue("duplicate_content", item_path, "Content hash belongs to multiple versions."))
                content_hashes[content_hash] = str(version_id)
            if not valid_storage_uri(record.get("storage_uri")):
                issues.append(issue("invalid_storage_uri", item_path, "storage_uri is invalid or contains credentials/query data."))
            if record.get("processing_status") not in PROCESSING_STATUSES:
                issues.append(issue("invalid_status", item_path, "processing_status is invalid."))
            if record.get("governance_status") not in GOVERNANCE_STATUSES:
                issues.append(issue("invalid_status", item_path, "governance_status is invalid."))
            if record.get("authority_status") not in AUTHORITY_STATUSES:
                issues.append(issue("invalid_status", item_path, "authority_status is invalid."))
            if not valid_timestamp(record.get("created_at")) or not valid_timestamp(record.get("updated_at")):
                issues.append(issue("invalid_timestamp", item_path, "created_at and updated_at must be ISO timestamps."))
            document_id = record.get("document_id")
            if record.get("governance_status") == "active" and isinstance(document_id, str):
                if document_id in active_documents:
                    issues.append(issue("multiple_active_versions", item_path, "Document has multiple active versions."))
                active_documents[document_id] = str(version_id)
                if record.get("processing_status") != "completed":
                    issues.append(issue("active_not_completed", item_path, "An active version must be completed."))

            source_items = record.get("source_occurrences")
            if not isinstance(source_items, list) or not source_items:
                issues.append(issue("missing_source_occurrence", item_path, "At least one source occurrence is required."))
                source_items = []
            for source_index, source in enumerate(source_items):
                source_path = f"{item_path}.source_occurrences[{source_index}]"
                if not isinstance(source, dict):
                    issues.append(issue("invalid_shape", source_path, "Source occurrence must be an object."))
                    continue
                for field in ("source_occurrence_id", "source_organization_id", "source_collection_id"):
                    if not valid_stable_id(source.get(field)):
                        issues.append(issue("invalid_id", source_path, f"{field} is invalid."))
                occurrence_id = source.get("source_occurrence_id")
                if isinstance(occurrence_id, str):
                    if occurrence_id in occurrences:
                        issues.append(issue("duplicate_id", source_path, "source_occurrence_id is duplicated."))
                    occurrences.add(occurrence_id)
                organization = organization_by_id.get(str(source.get("source_organization_id")))
                if organization is None:
                    issues.append(issue("unknown_organization", source_path, "Source organization is unknown."))
                elif record.get("governance_status") == "active" and organization.get("status") != "approved":
                    issues.append(issue("active_source_unapproved", source_path, "Active documents require approved source organizations."))
                original_path = source.get("original_relative_path")
                if not isinstance(original_path, str) or not original_path.strip():
                    issues.append(issue("missing_field", source_path, "original_relative_path is required."))
                else:
                    pure = PurePosixPath(original_path.replace("\\", "/"))
                    if pure.is_absolute() or ".." in pure.parts:
                        issues.append(issue("invalid_source_path", source_path, "Source path must be relative."))
                if not valid_timestamp(source.get("received_at")):
                    issues.append(issue("invalid_timestamp", source_path, "received_at must be an ISO timestamp."))

        for version_id, record in versions.items():
            superseded = record.get("supersedes_version_id")
            if superseded in (None, ""):
                continue
            target = versions.get(str(superseded))
            if target is None:
                issues.append(issue("unknown_superseded_version", str(self.registry_path), "Superseded version is unknown."))
            elif target.get("document_id") != record.get("document_id"):
                issues.append(issue("cross_document_supersedes", str(self.registry_path), "Supersedes crosses document identity."))
        for start in versions:
            seen: set[str] = set()
            current: str | None = start
            while current is not None and current in versions:
                if current in seen:
                    issues.append(issue("version_cycle", str(self.registry_path), "Supersedes relationship contains a cycle."))
                    break
                seen.add(current)
                next_value = versions[current].get("supersedes_version_id")
                current = str(next_value) if next_value not in (None, "") else None
        return issues

    def _validate_events(
        self,
        events: list[Any],
        current_revision: Any,
        path: str,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        for index, event in enumerate(events):
            item_path = f"{path}#events[{index}]"
            if not isinstance(event, dict):
                issues.append(issue("invalid_shape", item_path, "Governance event must be an object."))
                continue
            event_id = event.get("event_id")
            if not valid_stable_id(event_id):
                issues.append(issue("invalid_id", item_path, "event_id is invalid."))
            elif event_id in event_ids:
                issues.append(issue("duplicate_id", item_path, "event_id is duplicated."))
            else:
                event_ids.add(event_id)
            if not valid_timestamp(event.get("at")):
                issues.append(issue("invalid_timestamp", item_path, "Event timestamp is invalid."))
            if not isinstance(event.get("action"), str) or not event["action"].strip():
                issues.append(issue("missing_field", item_path, "Event action is required."))
            if not isinstance(event.get("actor"), str) or not event["actor"].strip():
                issues.append(issue("missing_field", item_path, "Event actor is required."))
            revision = event.get("registry_revision")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
                or (isinstance(current_revision, int) and revision > current_revision)
            ):
                issues.append(issue("invalid_revision", item_path, "Event revision is outside the registry history."))
        return issues

    def _validate_or_raise(self, organizations: dict[str, Any], registry: dict[str, Any]) -> None:
        issues = self.validate_state(organizations, registry)
        if issues:
            first = issues[0]
            raise GovernanceError(f"Governance validation failed [{first['code']}]: {first['message']}")

    def _event(self, action: str, actor: str, revision: int, **context: Any) -> dict[str, Any]:
        return {
            "event_id": f"event-{uuid4()}",
            "at": utc_now(),
            "action": action,
            "actor": actor,
            "registry_revision": revision,
            **context,
        }

    def _mutate(
        self,
        target: str,
        expected_revision: int,
        actor: str,
        action: str,
        operation: Callable[[dict[str, Any], dict[str, Any]], tuple[bool, dict[str, Any]]],
    ) -> dict[str, Any]:
        if expected_revision < 0:
            raise GovernanceError("expected_revision must be non-negative")
        if not actor.strip():
            raise GovernanceError("actor must not be empty")
        with self.mutation_lock(actor):
            organizations, registry = self.load_state()
            self._validate_or_raise(organizations, registry)
            document = organizations if target == "organizations" else registry
            current_revision = document.get("registry_revision")
            if current_revision != expected_revision:
                raise GovernanceError(
                    f"Revision conflict for {target}: expected {expected_revision}, current {current_revision}"
                )
            next_organizations = copy.deepcopy(organizations)
            next_registry = copy.deepcopy(registry)
            changed, result = operation(next_organizations, next_registry)
            if not changed:
                return {**result, "changed": False, "registry_revision": current_revision}
            selected = next_organizations if target == "organizations" else next_registry
            next_revision = current_revision + 1
            selected["registry_revision"] = next_revision
            events = selected.setdefault("events", [])
            if not isinstance(events, list):
                raise GovernanceError(f"{target} events must be a list")
            events.append(self._event(action, actor, next_revision, **result.get("event_context", {})))
            self._validate_or_raise(next_organizations, next_registry)
            atomic_write_json(self.organizations_path if target == "organizations" else self.registry_path, selected)
            output = {key: value for key, value in result.items() if key != "event_context"}
            return {**output, "changed": True, "registry_revision": next_revision}

    def add_organization(
        self,
        *,
        organization_id: str,
        name: str,
        aliases: list[str],
        status: str,
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            items = organizations.setdefault("organizations", [])
            existing = next((item for item in items if item.get("id") == organization_id), None)
            candidate = {"id": organization_id, "name": name.strip(), "status": status, "aliases": aliases}
            if existing == candidate:
                return False, {"organization_id": organization_id, "status": status}
            if existing is not None:
                raise GovernanceError(f"Organization already exists: {organization_id}")
            items.append(candidate)
            return True, {
                "organization_id": organization_id,
                "status": status,
                "event_context": {"organization_id": organization_id},
            }

        return self._mutate("organizations", expected_revision, actor, "organization_added", operation)

    def set_organization_status(
        self,
        *,
        organization_id: str,
        status: str,
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            organization = next(
                (item for item in organizations.get("organizations", []) if item.get("id") == organization_id), None
            )
            if organization is None:
                raise GovernanceError(f"Unknown organization: {organization_id}")
            old_status = organization.get("status")
            if old_status == status:
                return False, {"organization_id": organization_id, "status": status}
            organization["status"] = status
            return True, {
                "organization_id": organization_id,
                "status": status,
                "event_context": {"organization_id": organization_id, "from": old_status, "to": status},
            }

        return self._mutate("organizations", expected_revision, actor, "organization_status_changed", operation)

    def register(self, record: dict[str, Any], expected_revision: int, actor: str) -> dict[str, Any]:
        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            records = registry.setdefault("records", [])
            for existing in records:
                if existing.get("version_id") == record.get("version_id"):
                    comparable_existing = copy.deepcopy(existing)
                    comparable_record = copy.deepcopy(record)
                    for candidate in (comparable_existing, comparable_record):
                        candidate.pop("created_at", None)
                        candidate.pop("updated_at", None)
                        for occurrence in candidate.get("source_occurrences", []):
                            if isinstance(occurrence, dict):
                                occurrence.pop("received_at", None)
                    if comparable_existing == comparable_record:
                        return False, {
                            "document_id": record["document_id"],
                            "version_id": record["version_id"],
                        }
                    raise GovernanceError(f"version_id already exists: {record.get('version_id')}")
                if existing.get("content_sha256") == record.get("content_sha256"):
                    raise GovernanceError(
                        f"Content hash already registered as {existing.get('version_id')}; use add-source"
                    )
            records.append(record)
            return True, {
                "document_id": record["document_id"],
                "version_id": record["version_id"],
                "event_context": {
                    "document_id": record["document_id"],
                    "version_id": record["version_id"],
                },
            }

        return self._mutate("registry", expected_revision, actor, "document_registered", operation)

    def add_source(
        self,
        *,
        version_id: str,
        source: dict[str, Any],
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            record = next((item for item in registry.get("records", []) if item.get("version_id") == version_id), None)
            if record is None:
                raise GovernanceError(f"Unknown version: {version_id}")
            occurrences = record.setdefault("source_occurrences", [])
            existing = next(
                (item for item in occurrences if item.get("source_occurrence_id") == source["source_occurrence_id"]),
                None,
            )
            if existing == source:
                return False, {"document_id": record["document_id"], "version_id": version_id}
            if existing is not None:
                raise GovernanceError(f"source_occurrence_id already exists: {source['source_occurrence_id']}")
            occurrences.append(source)
            record["updated_at"] = utc_now()
            return True, {
                "document_id": record["document_id"],
                "version_id": version_id,
                "source_occurrence_id": source["source_occurrence_id"],
                "event_context": {
                    "document_id": record["document_id"],
                    "version_id": version_id,
                    "source_occurrence_id": source["source_occurrence_id"],
                },
            }

        return self._mutate("registry", expected_revision, actor, "source_added", operation)

    def set_status(
        self,
        *,
        version_id: str,
        processing_status: str | None,
        governance_status: str | None,
        authority_status: str | None,
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        if governance_status in {"active", "superseded"}:
            raise GovernanceError("Use activate to enter active/superseded states atomically")

        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            record = next((item for item in registry.get("records", []) if item.get("version_id") == version_id), None)
            if record is None:
                raise GovernanceError(f"Unknown version: {version_id}")
            changes: dict[str, dict[str, Any]] = {}
            for field, value in (
                ("processing_status", processing_status),
                ("governance_status", governance_status),
                ("authority_status", authority_status),
            ):
                if value is not None and value != record.get(field):
                    changes[field] = {"from": record.get(field), "to": value}
                    record[field] = value
            if not changes:
                return False, {"document_id": record["document_id"], "version_id": version_id}
            record["updated_at"] = utc_now()
            return True, {
                "document_id": record["document_id"],
                "version_id": version_id,
                "changes": changes,
                "event_context": {
                    "document_id": record["document_id"],
                    "version_id": version_id,
                    "changes": changes,
                },
            }

        return self._mutate("registry", expected_revision, actor, "document_status_changed", operation)

    def activate(self, *, version_id: str, expected_revision: int, actor: str) -> dict[str, Any]:
        def operation(organizations: dict[str, Any], registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            records = registry.get("records", [])
            target = next((item for item in records if item.get("version_id") == version_id), None)
            if target is None:
                raise GovernanceError(f"Unknown version: {version_id}")
            if target.get("governance_status") == "active":
                return False, {"document_id": target["document_id"], "version_id": version_id}
            if target.get("governance_status") == "withdrawn":
                raise GovernanceError("A withdrawn version cannot be activated")
            if target.get("processing_status") != "completed":
                raise GovernanceError("Only a completed version can be activated")
            organization_by_id = {
                item.get("id"): item for item in organizations.get("organizations", []) if isinstance(item, dict)
            }
            for source in target.get("source_occurrences", []):
                organization = organization_by_id.get(source.get("source_organization_id"))
                if organization is None or organization.get("status") != "approved":
                    raise GovernanceError("Every source organization must be approved before activation")
            current = next(
                (
                    item for item in records
                    if item.get("document_id") == target.get("document_id")
                    and item.get("governance_status") == "active"
                    and item.get("version_id") != version_id
                ),
                None,
            )
            prior = current.get("version_id") if current else None
            declared = target.get("supersedes_version_id")
            if current is not None and declared not in (None, prior):
                raise GovernanceError("Target supersedes_version_id does not match the current active version")
            now = utc_now()
            if current is not None:
                current["governance_status"] = "superseded"
                current["updated_at"] = now
                target["supersedes_version_id"] = prior
            target["governance_status"] = "active"
            target["updated_at"] = now
            return True, {
                "document_id": target["document_id"],
                "version_id": version_id,
                "superseded_version_id": prior,
                "event_context": {
                    "document_id": target["document_id"],
                    "version_id": version_id,
                    "superseded_version_id": prior,
                },
            }

        return self._mutate("registry", expected_revision, actor, "document_activated", operation)
