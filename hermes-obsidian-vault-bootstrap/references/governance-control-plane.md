# Engineering Governance Control Plane

The `engineering` profile creates the stage-1 document-governance control plane. It is intentionally
small and database-ready; stage-2 mutations are owned by Controlled Ingest's document-governance manager.

## Files and authority

| File | Purpose |
| --- | --- |
| `_system/vault.json` | Vault identity, security domain, readiness, storage default, and selected repository backend |
| `_system/metadata/document-governance.schema.json` | Stable record contract and planned relational mapping |
| `_system/metadata/source-organizations.json` | Controlled source-organization IDs and aliases |
| `_system/metadata/document-registry.json` | Current JSON repository for logical documents, immutable versions, resources, and source occurrences |

The current repository contract is `hermes-governance/v1`. `backend: json` means the JSON registry is
authoritative. The source files and Bundles remain evidence; Provider indexes remain rebuildable data.

## Identity rules

- `document_id` is stable across business versions of one logical document.
- `version_id` identifies one immutable content version.
- `resource_id` identifies the storage reference; moving from `local://` to a future `oss://` or `s3://`
  location must not change the document or version identity.
- `source_occurrence_id` preserves each independent delivery, even when its content hash is duplicated.
- IDs use lowercase letters, digits, `.`, `_`, and `-`; they are not display names or paths.

Source organizations have `candidate`, `approved`, or `retired` status. A model may propose a candidate,
but only a responsible reviewer can approve it; aliases must not resolve to multiple stable IDs.

Only one `active` version is allowed for a `document_id`. A `supersedes_version_id` must reference the
same document and cannot form a cycle. Processing, governance, and authority statuses are independent.

## Maintenance boundary

Bootstrap creates empty revision-0 registries with `readiness: draft`. It never overwrites or upgrades
existing governance files. Use the stage-2 manager for every post-bootstrap registry change and Vault
Lint for independent checking; ordinary agents must not infer or approve identities automatically.

Stage 2 provides `manage_document_governance.py` in Controlled Ingest as the governance
manager/repository entry point. Post-bootstrap writers must call it instead of editing either registry.
Every successful JSON mutation validates the complete next state, increments `registry_revision`, records
an actor-tagged event, and atomically replaces the old file. Automatic Bundle/Ingest integration remains
stage 3.

## Database preparation

The schema records the planned tables for SQLite/PostgreSQL. This is a mapping commitment, not an active
database. Do not create an unused database and do not dual-write JSON and SQL.

After the file-based stages and pilot are accepted, implement a second repository adapter, import a
revisioned JSON export, verify equivalent contract tests, and then switch `repository.backend`. Once SQL
is authoritative, Vault JSON may be retained only as an auditable export/snapshot. Credentials, database
files, migrations, and runtime caches stay outside the Vault.
