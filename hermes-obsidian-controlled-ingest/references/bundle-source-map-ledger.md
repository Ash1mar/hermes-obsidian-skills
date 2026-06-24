# Bundle Source Map and Section Ledger

Use this contract when Hermes ingests a Bundle v2 over one or more runs.

## Contents

1. [Purpose](#purpose)
2. [Control files](#control-files)
3. [Initialize or reconcile](#initialize-or-reconcile)
4. [Section lifecycle](#section-lifecycle)
5. [Controlled ingest procedure](#controlled-ingest-procedure)
6. [Change and stale detection](#change-and-stale-detection)
7. [Citation contract](#citation-contract)
8. [Source-map format](#source-map-format)
9. [Ledger schema](#ledger-schema)
10. [Failure and recovery rules](#failure-and-recovery-rules)

## Purpose

Bundle v2 keeps `document.md` compatible with the earlier Markdown workflow while adding navigation, provenance, assets, and evidence. Do not ingest the directory recursively.

Treat the files as three planes:

```text
control:  manifest.json + outline.json + section ledger
content:  selected non-overlapping document.md content_ranges
evidence: linked tables/images, then targeted _evidence only for QA
```

The source map is the human-readable Obsidian control page. The section ledger is the machine-readable state used for resumption, deduplication, revision checks, and stale detection.

## Control files

Create both files under the vault `_system/reports/` directory:

```text
<source-stem>.source-map.md
<source-stem>.section-ledger.json
```

Do not put them inside `10_Raw/`; they are governed reports, not raw source material.

The ledger is authoritative for section state. The source map is regenerated from the ledger and must not be edited as the state authority.

## Initialize or reconcile

Run:

```bash
python3 \
  hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  init "/path/to/input_document_bundle" \
  --reports-dir "/path/to/vault/_system/reports"
```

Initialization performs these operations without reading the full evidence archive:

1. Load and validate Bundle v2.
2. Record source, manifest, document, and outline hashes.
3. Create a stable `bundle_id` from `manifest.source.sha256`.
4. Copy section ids, titles, hierarchy, scope ranges, pages, asset ids, and quality from `outline.json`.
5. Derive non-overlapping `content_ranges` by subtracting each direct child's scope from its parent. These ranges are the ingest payload; the original scope remains navigation context.
6. Hash the normalized text owned by every section's `content_ranges`.
7. Write the JSON ledger atomically.
8. Generate the Markdown source map.

Run the same command again at the start of a later session. It reconciles the current Bundle against the existing ledger:

- unchanged section content preserves its state and outputs;
- changed completed/in-progress content becomes `stale`;
- changed pending content stays `pending`;
- a new section is added as `pending` or `qa_required`;
- a removed section moves to `orphaned_sections`;
- the ledger revision increments.

Use `--replace` only when deliberately discarding the existing ingestion state.

## Section lifecycle

Allowed statuses:

| Status | Meaning |
|---|---|
| `pending` | Ready for selection; no active write |
| `in_progress` | Claimed by an ingest run |
| `ingested` | Completed with at least one recorded output |
| `qa_required` | Blocked on extraction or evidence review |
| `skipped` | Deliberately excluded with a recorded reason |
| `stale` | Prior state or outputs may no longer match changed source content |

Normal transitions:

```text
pending -> in_progress -> ingested
pending -> qa_required -> in_progress
pending -> skipped
in_progress -> pending | qa_required | skipped
ingested -> qa_required | stale
stale -> in_progress | qa_required | skipped
skipped -> pending | in_progress
```

Do not use `--force-transition` during ordinary Hermes operation.

## Controlled ingest procedure

### 1. Reconcile and read status

Run `init` at the beginning of every new session, then inspect:

```bash
python3 hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  status "/path/to/vault/_system/reports/<source>.section-ledger.json"
```

Stop downstream writes when:

- ledger state is `blocked`;
- Bundle validation is `fail`;
- the intended section is `stale` and has not been reviewed;
- another run has left the same section `in_progress`.

### 2. Select one bounded section

Select by ledger `section.id`. Use its exact `content_ranges`, `pages`, and asset ids. `start_line` and `end_line` describe the full outline scope and may contain child sections; they are retained for navigation and citation context.

Parent and child sections are both safe ingest units because the ledger assigns each only its owned, non-overlapping lines. A section with `ingest_unit: false` has no owned lines and must not be claimed during ordinary operation.

### 3. Claim it with revision checking

Read the current ledger revision, then run:

```bash
python3 hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  update "/path/to/<source>.section-ledger.json" \
  --section "section-id" \
  --status in_progress \
  --expected-revision 7 \
  --note "Selected for controlled ingest"
```

A revision conflict means another process or session changed the ledger. Reload it instead of overwriting.

### 4. Read bounded content

Read and concatenate only the section's ordered `content_ranges` from `document.md`. Do not ingest the full parent scope when it encloses child scopes.

Follow only the table/image assets referenced by that section. Open `_evidence/` only to resolve a specific QA question.

### 5. Write governed artifacts

Apply the existing classification, routing, concept-governance, raw-source protection, and card limits. Every output must include the citation fields in this reference.

### 6. Complete or defer

Successful completion requires at least one output path:

```bash
python3 hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  update "/path/to/<source>.section-ledger.json" \
  --section "section-id" \
  --status ingested \
  --expected-revision 8 \
  --output "30_Cards/example.md" \
  --note "Created one bounded knowledge card"
```

If evidence is insufficient:

```bash
python3 hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  update "/path/to/<source>.section-ledger.json" \
  --section "section-id" \
  --status qa_required \
  --expected-revision 8 \
  --qa-item "Verify the cross-page table header against page evidence"
```

If deliberately excluded, use `skipped` and provide `--note`.

## Change and stale detection

The ledger uses four integrity levels:

```text
source SHA -> document SHA -> outline SHA -> per-section content SHA
```

- Different source SHA means a different bundle identity. Reconciliation refuses it.
- Different document or outline SHA triggers section comparison.
- Only sections whose exact content hash changed lose reusable completion state.
- Outputs from a stale section remain recorded for review; they are not silently deleted.
- Removed sections are retained under `orphaned_sections` for audit.

Do not mark a stale section ingested without re-reading its current `content_ranges` and reviewing its prior outputs.

## Citation contract

Every note created from Bundle v2 must record at least:

```yaml
source_bundle_id: bundle-v2-<source-sha-prefix>
source_sha256: <full source sha256>
source_section_id: <outline section id>
source_lines: <start>-<end>
source_pages:
  - 12
  - 13
source_assets: []
```

When a table or image is used, add its stable asset id to `source_assets` and link the normalized asset path.

Do not cite `_evidence/blocks.jsonl`, layout PDFs, or MinerU intermediate JSON as the normalized source. Cite them only in a QA note that explains what was checked.

## Source-map format

The generated Markdown contains:

- bundle id, source/document hashes, validation state, and ledger revision;
- Bundle path and ledger link;
- Bundle-level QA requirements;
- one row per outline section with state, owned line ranges, enclosing scope, pages, quality, and outputs;
- QA/stale section details;
- the citation contract.

The source map is safe for normal Obsidian reading because it does not duplicate the full document or evidence archive.

## Ledger schema

Top-level fields:

```text
ledger_schema_version
bundle_id
revision
state
created_at / updated_at
control
bundle
source
conversion
validation
review_required
sections
orphaned_sections
history
summary
```

Each section records:

```text
id / title / hierarchy
start_line / end_line / content_ranges / pages / assets
quality / content_sha256
status / previous_status
outputs / qa_items / notes
started_at / completed_at / updated_at
```

Top-level states:

- `active`: work remains and no global failure exists;
- `blocked`: Bundle validation failed;
- `stale`: at least one section requires reconciliation review;
- `complete`: every current section with `ingest_unit: true` is `ingested` or `skipped`.

## Failure and recovery rules

- Atomic writes use a sibling temporary file and replace the ledger only after serialization succeeds.
- A crashed run may leave a section `in_progress`; do not claim it blindly. Inspect the ingest log and either finish it or return it to `pending` with a note.
- Always pass `--expected-revision` for section updates.
- Do not hand-edit the revision or content hashes.
- If the ledger is corrupt, preserve it as audit evidence and rebuild with `init --replace`; do not silently overwrite it.
- If Bundle validation changes to `fail`, the top-level state becomes `blocked` even if prior sections were ingested.
- The ledger records paths to governed outputs but does not delete or rewrite those files.
