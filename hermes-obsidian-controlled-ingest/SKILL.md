---
name: hermes-obsidian-controlled-ingest
description: Governed Obsidian vault ingestion for Markdown, engineering PDFs, layered MinerU document bundles, and other source files. Use when asked to onboard a new external source, detect whether a vault/source/bundle/ledger is new or resumable, preserve raw sources, initialize or recover local MinerU Bundle v2 processing, classify material, create source maps, manage section ledgers, ingest long documents by bounded ranges, perform batch synthesis across related documents, generate high-confidence cards or candidate concepts, prevent duplicate or stale section ingestion, route cards/concepts/projects/spec indexes, reconcile new evidence with existing artifacts, validate or convert engineering PDFs, retain page/table/figure evidence, enforce quality gates, maintain Dataview metadata, or record ingest logs and source maps.
---

# Hermes Obsidian Controlled Ingest

Turn source files into governed Obsidian artifacts without rewriting raw material or overcreating concepts.

```text
external or vault source
-> runtime state detection
-> 10_Raw preservation
-> recovery/resume or layered PDF bundle
-> material classification
-> source map or bounded ingestion
-> governed artifact
-> incremental reconciliation
-> batch synthesis when applicable
-> ingest log
```

## First Checks

Before writing:

1. Read the vault `AGENTS.md` and `_system/prompts/hermes-ingest-rules.md` when present.
2. Read `_system/metadata/concept-registry.md` before creating or linking concepts.
3. Inspect `30_Cards/`, `40_Concepts/`, `50_Projects/`, and `_system/reports/`.
4. Treat existing content in `10_Raw/` as read-only.
5. Preserve the source path in every derived artifact.

See `references/vault-structure.md` for the validated vault layout and artifact templates.

## Runtime State Detection

Before processing any source or batch, classify the current state and choose the next action:

| State | Detection | Next action |
| --- | --- | --- |
| new vault required | vault path missing or lacks governed structure | use `hermes-obsidian-vault-bootstrap` first |
| existing vault, new external source | source is outside vault and no matching `10_Raw/` copy exists | copy to `10_Raw/`, verify SHA-256, then build Bundle v2 when needed |
| raw exists, no bundle | matching raw file exists, no corresponding `10_Raw/converted/*_document_bundle` | build Bundle v2 from the vault raw copy |
| bundle exists but is empty or invalid | bundle directory exists but `manifest.json` or `document.md` is missing, empty, or validation fails | treat as failed derived output; use recovery rules |
| valid bundle, no ledger/source map | Bundle validates as `pass` or `warn`, control files missing | initialize source map and section ledger |
| ledger/source map exists | control files exist | run ledger init to reconcile, then resume by ledger state |
| prior source ingestion complete | ledger has terminal `ingested`/`skipped`/`qa_required`, no `pending`/`in_progress`/`stale` | run reconciliation or batch synthesis instead of repeating ingestion |

Do not rely on filenames alone. Use raw SHA-256, Bundle validation, and ledger revision as the state authority.

## New Source Onboarding

When the source is outside the vault:

1. Require an initialized governed vault. If it does not exist, use `hermes-obsidian-vault-bootstrap` first.
2. Copy the source unchanged into `10_Raw/`; never overwrite a conflicting file.
3. Verify the copied source against the original by SHA-256, then treat it as read-only.
4. Run all conversion from the vault copy and write derived output under `10_Raw/converted/`.
5. For a new engineering PDF or complex manual, create a fresh Bundle v2 with the configured local MinerU path. Do not reuse prior MinerU output or a prior bundle unless the user explicitly requests reuse or resumption.
6. Stop and report instead of substituting a weaker conversion when the required MinerU path is unavailable or Bundle validation fails.

## Recovery and Resume Rules

- Reuse a raw file only when its SHA-256 matches the external source; stop on mismatch.
- Never treat an empty bundle directory as completed work.
- Never skip conversion only because a bundle folder exists.
- A usable Bundle must pass `scripts/validate_document_bundle.py` as `pass` or `warn`; `fail` is not usable.
- If a Bundle validates as `warn`, continue with QA restrictions and do not promote affected formulas, tables, figures, or parameters as authoritative facts.
- If a Bundle is empty, missing required files, or fails validation, remove or replace only the derived bundle output, never the raw source, and retry once with a skill-supported MinerU parameter change such as `pipeline` backend or `txt` method when available.
- If retry still fails, record the failed source, raw SHA, bundle path, command, validator output, and recommended manual check.
- If a valid Bundle and ledger already exist, reconcile and resume; do not rebuild unless the user explicitly asks for fresh conversion.
- If source ingestion is already complete, proceed to incremental reconciliation or batch synthesis rather than repeating section ingestion.

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
4. Use recovery rules when validation returns `fail`; stop at an ingest log or QA report when recovery is unavailable or retry fails.
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

## Incremental Reconciliation

After each bounded ingest unit, and after each source in a batch:

1. Review new outputs against existing cards, concepts, project notes, spec indexes, Dataview metadata, and registries.
2. Prefer updating links, citations, relationship fields, source lists, and review notes over creating near-duplicate artifacts.
3. If new evidence clearly changes an existing card or concept, update it with citation-backed edits.
4. If the change is uncertain, affects concept boundaries, or implies a merge/rename, write a candidate review or QA item instead of restructuring silently.
5. Mark stale or review-required outputs when prior conclusions may no longer match the expanded source evidence.
6. Record created, updated, reused, skipped, stale, and review-required artifacts in the section ledger and ingest report.
7. Do not delete prior outputs or silently rewrite established concepts during batch ingestion.
8. Report reconciliation evidence: paths inspected, matching existing artifacts, decision rationale, and citations used for any update.

## Batch Synthesis Phase

After source ingestion for a batch, automatically synthesize when two or more related sources have valid ledgers:

1. Select high-value sections from successful ledgers: `ingested`, non-QA, non-skipped, and citation-ready.
2. Compare related sources for repeated system objects, equipment, operating parameters, design criteria, interfaces, validation rules, and terminology.
3. Generate a small number of high-confidence `30_Cards/` when evidence is clear across one or more sources. Each card must cite source PDF, bundle id, section id, pages or lines, and QA exclusions.
4. Create candidate concept reviews for stable cross-source objects with reusable value. Create actual concept pages only when boundaries are clear and registry checks pass.
5. Do not let bundle-level `warn` block synthesis. Exclude affected QA sections and use only unaffected evidence.
6. If no card or candidate concept is created, justify that with inspected section ids and evidence gaps; do not only say the material is long or engineering-dense.
7. Update existing cards, spec indexes, registry notes, or Dataview metadata when new evidence changes links, sources, relationships, or stale/review-required status.

## Long and Complex Sources

For a long, composite, or engineering-dense source:

1. Initialize or reconcile the generated source map and section ledger first.
2. During first source ingestion, prefer source map, ledger, and spec index before cards or concepts.
3. Select later work by ledger section and its non-overlapping `document.md` `content_ranges`.
4. Load tables and figures only for the selected range.
5. Limit cards per run.
6. Put candidate concepts through review before creation.
7. Keep unverified formulas and parameters in the source map as QA items, not facts.
8. Resume from ledger status and revision; do not infer completion from existing filenames alone.
9. After the controlled source layer is complete, run batch synthesis when related completed sources exist.

## Batch Ingestion

Do not batch ingest mixed material blindly.

1. Create a batch pre-scan plan.
2. Group files by material type and bundle quality.
3. Process one file at a time unless the user explicitly requests a different unit.
4. Complete incremental reconciliation for the current file before moving to the next file.
5. After each group, run batch synthesis for related valid ledgers before declaring the batch complete.
6. Avoid mixing long theory, engineering specifications, project documents, and tool observations in one write run.

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
11. Existing cards, concepts, projects, spec indexes, or Dataview metadata updated or marked stale/review-required.
12. Batch synthesis decisions: sections compared, cards created or updated, candidate concepts, and rejected candidates.
13. Failed bundle recovery attempts and remaining manual checks.
14. Extraction QA items and recommended next step.
