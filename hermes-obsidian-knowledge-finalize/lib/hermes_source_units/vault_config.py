"""Portable bootstrap contract with P4 Vault Finalize capability."""
import json
import re
from pathlib import Path

from .validation import default_config, fingerprint, validate_record

CONFIG_PATH = "_system/metadata/source-unit-config.json"
DIRECTORIES = ["_system/sources/artifacts", "_system/sources/sections",
               "_system/sources/units", "_system/ledgers/unit-work",
               "_system/knowledge-builds", "_system/knowledge-releases",
               "_system/navigation"]
CONTRACTS = [
    "hermes-source-unit-config/v2", "hermes-normalized-artifact/v1",
    "hermes-source-section/v1", "hermes-source-unit/v1", "hermes-source-unit-set/v2",
    "hermes-source-unit-current/v1", "hermes-chunk-engine-report/v1",
    "hermes-unit-work-ledger/v1", "hermes-reading-window/v1",
    "hermes-reading-package/v1", "hermes-knowledge-pass/v1",
    "hermes-knowledge-identity-registry/v1", "hermes-knowledge-page-revision/v1",
    "hermes-knowledge-build-run/v1", "hermes-knowledge-build/v4",
    "hermes-vault-finalize-plan/v1", "hermes-knowledge-navigation/v1",
    "hermes-knowledge-release-state/v1", "hermes-knowledge-release/v1",
    "hermes-source-unit-capability/v1",
]
NOTICE = """\n## Source-unit rollout gate (P4)

This Vault uses the new source-unit architecture. Bootstrap initializes identity,
storage directories and validated configuration. Controlled ingest can prepare,
build and exactly read source units, run Pass/Reduce and Build Finalize, then publish
an auditable P4 knowledge release through explicit Vault Finalize. Provider indexing
is not connected yet. Do not run the legacy knowledge/query path
against this Vault or claim it is query-ready. Wait for the P5 implementation.
Knowledge citations resolve immutable unit references with exact
source spans; appended reading context is not original evidence.
"""


def configuration(override=None):
    """Explicit complete JSON config wins; otherwise use packaged defaults.

    No implicit environment, host configuration or partial merge is consulted.
    """
    value = (json.loads(Path(override).read_text(encoding="utf-8"))
             if override else default_config())
    validate_record("config", value)
    return value


def declaration(config):
    validate_record("config", config)
    return {"contract": "hermes-source-unit-vault/v1", "phase": "P4",
            "config_path": CONFIG_PATH, "config_fingerprint": fingerprint(config),
            "contracts": CONTRACTS, "directories": DIRECTORIES,
            "capabilities": {"bootstrap": True, "source_reader": True,
                             "knowledge_build": True, "vault_finalize": True,
                             "retrieval": False}}


def validate_vault(vault):
    """Read-only scaffold validation, deliberately separate from query readiness."""
    vault = Path(vault).resolve()
    def local(relative):
        path = vault / relative
        if not path.resolve().is_relative_to(vault):
            raise ValueError(f"Vault path escapes root: {relative}")
        return path
    manifest = json.loads(local("_system/vault.json").read_text(encoding="utf-8"))
    value = configuration(local(CONFIG_PATH))
    if manifest.get("source_units") != declaration(value):
        raise ValueError("Source-unit declaration/config fingerprint or P4 capability mismatch")
    identity = manifest.get("vault", {})
    if not isinstance(identity, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", str(identity.get("id", ""))):
        raise ValueError("Missing Vault identity")
    for relative in ("_system/metadata/document-registry.json",
                     "_system/metadata/source-organizations.json",
                     "_system/metadata/document-governance.schema.json"):
        if not isinstance(json.loads(local(relative).read_text(encoding="utf-8")), dict):
            raise ValueError(f"Invalid governance object: {relative}")
    identities = json.loads(local("_system/metadata/knowledge-identities.json").read_text(encoding="utf-8"))
    validate_record("identity_registry", identities)
    releases = json.loads(local("_system/metadata/knowledge-release-state.json").read_text(encoding="utf-8"))
    validate_record("knowledge_release_state", releases)
    for relative in DIRECTORIES:
        if not local(relative).is_dir():
            raise ValueError(f"Missing source-unit directory: {relative}")
    return {"ok": True, "phase": "P4", "bootstrap_ready": True,
            "query_ready": False, "config_fingerprint": fingerprint(value),
            "effective_config": value}
