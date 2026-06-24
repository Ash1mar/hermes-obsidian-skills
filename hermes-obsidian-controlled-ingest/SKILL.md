---
name: hermes-obsidian-controlled-ingest
description: Governed Obsidian vault ingestion for Markdown, engineering PDFs, layered MinerU document bundles, and other source files. Use when asked to preserve raw sources, classify material, create source maps, manage resumable section ledgers, ingest long documents by bounded ranges, prevent duplicate or stale section ingestion, route cards/concepts/projects/spec indexes, validate or convert engineering PDFs, retain page/table/figure evidence, enforce quality gates, maintain Dataview metadata, or record ingest logs and source maps.
---

# Hermes Obsidian Controlled Ingest

Turn source files into governed Obsidian artifacts without rewriting raw material or overcreating concepts.

```text
source
-> optional conversion or layered PDF bundle
-> 10_Raw preservation
-> material classification
-> source map or bounded ingestion
-> governed artifact
-> ingest log
```

## First Checks

Before writing:

1. Read the vault `AGENTS.md` and `_system/prompts/hermes-ingest-rules.md` when present.
2. Read `_system/metadata/concept-registry.md` before creating or linking concepts.
3. Inspect `30_Cards/`, `40_Concepts/`, `50_Projects/`, and `_system/reports/`.
4. Treat `10_Raw/` as read-only.
5. Preserve the source path in every derived artifact.

See `references/vault-structure.md` for the validated vault layout and artifact templates.

## Source Handling

Process Markdown directly from `10_Raw/`.

For an engineering PDF or complex manual, create a layered MinerU bundle under `10_Raw/converted/` with:

```text
document_bundle/
  manifest.json
  document.md
  outline.json
  images/
  tables/
  _evidence/
```

Use `scripts/convert_pdf_with_mineru_bundle.py`. Use `--model-source local` in the repaired offline MinerU environment. Read `references/mineru-pdf-bundle.md` before conversion or validation. Read `references/bundle-source-map-ledger.md` before staged or multi-session ingestion.

For Word, PowerPoint, Excel, HTML, CSV, JSON, XML, image, audio, EPUB, ZIP, URL, or a simple PDF when MinerU is unavailable:

1. Convert the source to Markdown with `scripts/convert_with_markitdown.py` when available.
2. Save converted Markdown under `10_Raw/converted/`.
3. Keep the original unchanged.
4. Record conversion metadata in the ingest log.

See `references/markitdown.md` and `references/mcp-markitdown.md` when using those paths.

## Layered Bundle Ingestion

Keep the agent-facing path small. Do not recursively scan a bundle.

1. Read `manifest.json` first.
2. Run `scripts/validate_document_bundle.py <bundle>` before downstream writes.
3. Run `scripts/manage_bundle_ingest.py init <bundle> --reports-dir <vault>/_system/reports` at the start of every session. This creates or reconciles the source map and section ledger.
4. Stop at an ingest log or QA report when validation returns `fail` or the ledger state is `blocked`.
5. Allow a source map when status is `warn`, but do not promote affected formulas, tables, figures, or parameters as authoritative facts.
6. Select an eligible ledger section. Do not duplicate an `ingested` section or reuse a `stale` section without review.
7. Claim the section as `in_progress` with `--expected-revision` before downstream writes.
8. Read only its ledger `content_ranges` from `document.md`. The enclosing `start_line`/`end_line` scope is context, not an instruction to duplicate nested child content.
9. Follow linked files under `tables/` or `images/` only when the selected section needs them.
10. Record every created output and finish the section as `ingested`, `qa_required`, or `skipped`. Never leave a successful run only in prose logs.
11. Do not read `_evidence/` by default. Open it only for targeted QA of layout, page order, formulas, tables, or extraction disputes.

Treat `document.md` as the single normalized text source. Do not duplicate every section into separate Markdown files. Use the ledger's non-overlapping `content_ranges` for staged ingestion and the JSON ledger as the section-state authority.

Honor `manifest.quality.review_required`. Verify engineering formulas, table structure, and figure internals against page evidence before turning them into reusable rules or parameters.

## Material Classification

Classify from content evidence before choosing outputs. Common classifications include:

- short knowledge note
- method or framework material
- implementation guide
- project material or project package
- engineering handbook or specification
- field or terminology specification
- quality or validation rule
- long or composite source
- source-map-only material
- unsuitable for ingestion

Explain the classification before routing artifacts.

## Artifact Routing

| Judgment | Artifact | Folder |
| --- | --- | --- |
| reusable knowledge idea | knowledge card | `30_Cards/` |
| stable reusable concept | concept page | `40_Concepts/` |
| project package or implementation plan | project note | `50_Projects/` |
| engineering handbook, field, specification, or rule | spec/handbook index or report | `_system/reports/` |
| long or composite source | source map first | `_system/reports/` |
| dashboard/query view | Dataview note | `90_Dataview/` |
| uncertain, unsafe, or failed extraction | ingest/QA log only | `_system/reports/` |

For engineering handbooks, prefer a document index and staged chapter plan. Do not default to a generic knowledge card.

## Concept Governance

Do not create concept pages by default. Before creating one, answer:

- Does `concept-registry.md` already cover it?
- Is it only a tool, product, model, field, module, step, chapter, parameter, or one-off feature?
- Is it an implementation case or workflow extension of an existing concept?
- Does it have cross-file reuse value?
- Is current evidence sufficient and quality-gated?
- Is its boundary with existing concepts clear?

If unsure, write a candidate concept review instead. Use relationship types:

- `core`
- `implementation-case`
- `workflow-extension`
- `indirect`
- `contrast`
- `weak`

See `references/concept-governance.md`.

## Long and Complex Sources

For a long, composite, or engineering-dense source:

1. Initialize or reconcile the generated source map and section ledger first.
2. Do not create cards or concepts in the same run unless explicitly requested.
3. Select later work by ledger section and its non-overlapping `document.md` `content_ranges`.
4. Load tables and figures only for the selected range.
5. Limit cards per run.
6. Put candidate concepts through review before creation.
7. Keep unverified formulas and parameters in the source map as QA items, not facts.
8. Resume from ledger status and revision; do not infer completion from existing filenames alone.

## Batch Ingestion

Do not batch ingest mixed material blindly.

1. Create a batch pre-scan plan.
2. Group files by material type and bundle quality.
3. Process one group at a time.
4. Avoid mixing long theory, engineering specifications, project documents, and tool observations in one write run.

## Required Run Summary

Report every run with:

1. Whether `10_Raw/` remained unchanged.
2. Material classification and evidence.
3. Bundle schema/profile and quality status when applicable.
4. Bundle id, ledger revision, and section status transition when applicable.
5. Exact sections, line ranges, tables, and figures read.
6. Files created and recorded in the section ledger.
7. Possible artifacts not created and why.
8. Whether any concept page or registry entry changed and why.
9. Existing concepts reused and relationship types.
10. Candidate concepts and decisions.
11. Extraction QA items and recommended next step.
