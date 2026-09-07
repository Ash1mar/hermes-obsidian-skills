# Document Governance Manager

Use this command when `_system/vault.json` enables the `hermes-governance/v1` JSON
repository. It is the sole supported mutation path for the source-organization and document registries.
Vault Lint remains an independent read-only audit.

## Safety contract

- Read the relevant `registry_revision` immediately before a mutation and pass it as
  `--expected-revision`.
- Supply a meaningful human or service identity with `--actor`.
- Do not delete a `.document-governance.lock` until an operator has confirmed that no writer is running.
- Do not hand-edit either registry. A failed operation leaves its target file unchanged.
- Organization and document revisions are independent. Organization commands use the organization
  revision; document commands use the document-registry revision.
- A model may add a candidate organization only when that action is authorized, but approval and
  document activation require responsible human judgment.

All examples use:

```bash
python3 "<ingest-skill-root>/scripts/manage_document_governance.py" --vault "/path/to/Vault"
```

Append the command and its arguments to that prefix.

## Governed ingest lifecycle

After an unchanged source has been copied into `10_Raw/`, register it as processing. IDs are explicit;
the command derives only the Vault-relative storage URI and SHA-256 from the actual raw file.

```bash
ingest-start \
  --raw-source "/path/to/Vault/10_Raw/shared-specification.pdf" \
  --document-id doc-shared-specification \
  --version-id version-shared-2026-01 \
  --collection-id collection-engineering \
  --title "Shared specification" \
  --business-version "2026-01" \
  --resource-id resource-shared-2026-01 \
  --authority-status official \
  --source-occurrence-id occurrence-owner-2026-01 \
  --source-organization-id organization-owner \
  --source-collection-id collection-owner-delivery \
  --original-relative-path "incoming/shared-specification.pdf" \
  --expected-revision 0 \
  --actor ingest-run-id \
  --json
```

After producing Bundle v2, finish processing with the revision returned above:

```bash
ingest-finish \
  --bundle "/path/to/Vault/10_Raw/converted/shared_document_bundle" \
  --version-id version-shared-2026-01 \
  --expected-revision 1 \
  --actor ingest-run-id \
  --json
```

The finish command validates the Bundle and compares `manifest.source.sha256` with the immutable version
hash. A pass or warn result sets `processing_status: completed`; a fail result sets it to `failed`.
Hash mismatch changes neither registry nor manifest. It then writes `manifest.governance` containing only
the contract, Vault/document/version/resource IDs, registry path, and observed revision. This is a
rebuildable projection, not another source of truth. A retry at the current revision repairs a missing
projection without adding an event when processing state is already correct.

For a governed Vault, `manage_bundle_ingest.py init` refuses to create or reconcile the source map and
section ledger until that projection resolves to the same registry record, source hash, and required
processing status. Legacy Vaults without `_system/vault.json` retain the old behavior.

## Inspect and validate

```bash
validate --json
```

This returns both revisions, object counts, and structured issues. It does not write.

## Source organizations

Create a candidate first:

```bash
organization-add \
  --organization-id organization-owner \
  --name "Owner Organization" \
  --alias "Owner" \
  --status candidate \
  --expected-revision 0 \
  --actor operator-id \
  --json
```

After review, approve it with the revision returned by the prior operation:

```bash
organization-status \
  --organization-id organization-owner \
  --status approved \
  --expected-revision 1 \
  --actor reviewer-id \
  --json
```

An organization referenced by an active version cannot be retired while that would invalidate the
active document state.

## Register a version

Registration requires explicit stable IDs. Do not derive `document_id` solely from a path or filename.
The initial governance state cannot be `active` or `superseded`; activation is a separate atomic action.

```bash
register \
  --document-id doc-shared-specification \
  --version-id version-shared-2026-01 \
  --collection-id collection-engineering \
  --title "Shared specification" \
  --business-version "2026-01" \
  --resource-id resource-shared-2026-01 \
  --storage-uri "local://10_Raw/shared-specification.pdf" \
  --content-sha256 "<64-lowercase-hex>" \
  --processing-status completed \
  --authority-status official \
  --source-occurrence-id occurrence-owner-2026-01 \
  --source-organization-id organization-owner \
  --source-collection-id collection-owner-delivery \
  --original-relative-path "incoming/shared-specification.pdf" \
  --expected-revision 0 \
  --actor ingest-run-id \
  --json
```

`storage_uri` currently accepts credential-free `local://`, `oss://`, or `s3://` references. This records
identity only; remote storage access is not implemented.

If the same SHA-256 is already registered, use `add-source` on that existing version. Do not create a
second searchable version merely because another organization delivered the same bytes.

## Add an independent source occurrence

```bash
add-source \
  --version-id version-shared-2026-01 \
  --source-occurrence-id occurrence-partner-2026-01 \
  --source-organization-id organization-partner \
  --source-collection-id collection-partner-delivery \
  --original-relative-path "partner/shared-specification.pdf" \
  --expected-revision 1 \
  --actor ingest-run-id \
  --json
```

## Change non-activation status

```bash
status \
  --version-id version-shared-2026-01 \
  --processing-status completed \
  --authority-status official \
  --expected-revision 1 \
  --actor reviewer-id \
  --json
```

At least one status option is required. `active` and `superseded` cannot be set here.

## Activate a reviewed version

```bash
activate \
  --version-id version-shared-2026-01 \
  --expected-revision 2 \
  --actor reviewer-id \
  --json
```

Activation requires completed processing and approved source organizations. If another version of the
same logical document is active, the command changes that version to `superseded`, links the new version
to it with `supersedes_version_id`, activates the target, and commits the whole change in one registry
revision.

## Failure handling

Exit code `2` means validation, revision, lock, identity, or transition failure. Re-read both registries,
resolve the reported issue, and retry with their current revisions. Never bypass a conflict by manually
editing `registry_revision`.
