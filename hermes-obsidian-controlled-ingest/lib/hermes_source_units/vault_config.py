"""Portable bootstrap contract with P2 source-plane capability."""
import json
import re
from pathlib import Path

from .validation import default_config, fingerprint, validate_record

CONFIG_PATH = "_system/metadata/source-unit-config.json"
DIRECTORIES = ["_system/sources/artifacts", "_system/sources/sections",
               "_system/sources/units", "_system/ledgers/unit-work",
               "_system/knowledge-builds"]
CONTRACTS = ["hermes-source-unit-config/v1", "hermes-unit-work-ledger/v1",
             "hermes-knowledge-build/v4", "hermes-source-unit-capability/v1"]
NOTICE = """\n## Source-unit rollout gate (P2)

This Vault uses the new source-unit architecture. Bootstrap initializes identity,
storage directories and validated configuration. Controlled ingest can prepare,
build and exactly read source units; knowledge construction and Provider indexing
are not connected yet. Do not run the legacy knowledge/query path against this Vault
or claim it is query-ready. Wait for the corresponding P3/P5 implementations.
Knowledge citations must eventually resolve immutable unit references with exact
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
    return {"contract": "hermes-source-unit-vault/v1", "phase": "P2",
            "config_path": CONFIG_PATH, "config_fingerprint": fingerprint(config),
            "contracts": CONTRACTS, "directories": DIRECTORIES,
            "capabilities": {"bootstrap": True, "source_reader": True,
                             "knowledge_build": False, "retrieval": False}}


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
        raise ValueError("Source-unit declaration/config fingerprint or P2 capability mismatch")
    identity = manifest.get("vault", {})
    if not isinstance(identity, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", str(identity.get("id", ""))):
        raise ValueError("Missing Vault identity")
    for relative in ("_system/metadata/document-registry.json",
                     "_system/metadata/source-organizations.json",
                     "_system/metadata/document-governance.schema.json"):
        if not isinstance(json.loads(local(relative).read_text(encoding="utf-8")), dict):
            raise ValueError(f"Invalid governance object: {relative}")
    for relative in DIRECTORIES:
        if not local(relative).is_dir():
            raise ValueError(f"Missing source-unit directory: {relative}")
    return {"ok": True, "phase": "P2", "bootstrap_ready": True,
            "query_ready": False, "config_fingerprint": fingerprint(value),
            "effective_config": value}
