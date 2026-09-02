---
name: hermes-obsidian-controlled-ingest
description: 受控摄取 / Controlled Ingest：新增、导入、恢复、继续或批量处理受治理 Obsidian Vault 的 PDF、图片及其他来源时使用，包括保护 10_Raw、转换、质量门禁、source map、section ledger、证据定位、知识合成、双链和查询回写。On Hermes, MUST call skill_view for hermes-obsidian-controlled-ingest and load its complete scripts before ingesting; on other runtimes, load the full skill first.
---

# Hermes Obsidian Controlled Ingest

Turn source files into governed Obsidian artifacts without rewriting raw material or overcreating concepts.

## Deployment Profile

Read optional `config/deployment.json` before selecting runtime paths or MinerU transport. When
present, use its `vault_path` as the governed Vault default, treat `hermes_skills_root` only as a
deployment consistency check, and honor its `mineru_invocation` and `mineru_api_url`. Explicit CLI
arguments may override packaged defaults for a deliberate operation; prompt wording alone must not
silently switch a configured Vault or service. When the file is absent, require the Vault from the
user/task context and keep the local MinerU CLI fallback.

## Runtime Skill Boundary

Use `<ingest-skill-root>` as the runtime-neutral directory containing this active `SKILL.md`, not the parent directory that contains multiple Skills. The package layout is `<ingest-skill-root>/SKILL.md`, `<ingest-skill-root>/scripts/*.py`, `<ingest-skill-root>/references/*.md`, and optional `<ingest-skill-root>/config/*.json`. Resolve it from the active runtime's loader. On Hermes, use the concrete expanded `${HERMES_SKILL_DIR}` or the `skill_dir` returned by `skill_view(name="hermes-obsidian-controlled-ingest")`; on another runtime, use its equivalent active-skill directory.

Resolve bundled `scripts/`, `references/`, and `config/` against `<ingest-skill-root>`. Execute Python entry points as `python3 "<ingest-skill-root>/scripts/<script>.py"`. Never infer a conventional installation path. On Hermes, inspect `skill_view` and `linked_files.scripts` before declaring a script uninstalled; a terminal sandbox path failure may instead mean the host Skill directory is not mounted. Never copy replacement Skill scripts into the Vault.

```text
external or vault source
-> runtime state detection
-> 10_Raw preservation
-> recovery/resume or layered PDF/image bundle
-> material classification
-> source map or bounded ingestion
-> governed artifact
-> incremental reconciliation
-> batch synthesis when applicable
-> ingest log

query-writeback candidate
-> candidate validation
-> source evidence re-check
-> duplicate/reuse check
-> governed writeback or skip
-> writeback log
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
| raw exists, no bundle | matching raw file exists, no corresponding `10_Raw/converted/*_document_bundle` or `*_image_document_bundle` | build Bundle v2 from the vault raw copy |
| bundle exists but is empty or invalid | bundle directory exists but `manifest.json` or `document.md` is missing, empty, or validation fails | treat as failed derived output; use recovery rules |
| valid bundle, no ledger/source map | Bundle validates as `pass` or `warn`, control files missing | initialize source map and section ledger |
| ledger/source map exists | control files exist | run ledger init to reconcile, then resume by ledger state |
| prior source ingestion complete | ledger has terminal `ingested`/`skipped`/`qa_required`, no `pending`/`in_progress`/`stale` | run reconciliation or batch synthesis instead of repeating ingestion |
| query-writeback candidate | input is a candidate from `_system/reports/query-writeback-candidates/` or current conversation | run query-derived writeback; do not treat the query answer as source evidence |

Do not rely on filenames alone. Use raw SHA-256, Bundle validation, and ledger revision as the state authority.

## New Source Onboarding

When the source is outside the vault:

1. Require an initialized governed vault. If it does not exist, use `hermes-obsidian-vault-bootstrap` first.
2. Copy the source unchanged into `10_Raw/`; never overwrite a conflicting file.
3. Verify the copied source against the original by SHA-256, then treat it as read-only.
4. Run all conversion from the vault copy and write derived output under `10_Raw/converted/`.
5. For a new engineering PDF, complex manual, or standalone image source that carries source content, create a fresh Bundle v2 from the vault raw copy. Do not reuse prior conversion output or a prior bundle unless the user explicitly requests reuse or resumption.
6. Stop and report instead of substituting a weaker conversion when the required MinerU path is unavailable or Bundle validation fails.

## Recovery and Resume Rules

- Reuse a raw file only when its SHA-256 matches the external source; stop on mismatch.
- Never treat an empty bundle directory as completed work.
- Never skip conversion only because a bundle folder exists.
- A usable Bundle must pass `python3 "<ingest-skill-root>/scripts/validate_document_bundle.py"` as `pass` or `warn`; `fail` is not usable.
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

Use `python3 "<ingest-skill-root>/scripts/convert_pdf_with_mineru_bundle.py"`. The helper uses the local MinerU CLI by default; a deployment config or explicit `--mineru-api-url` may select the HTTP transport, which must return a ZIP containing Markdown, content lists, middle/model JSON, and referenced images. Use `--model-source local` in the repaired offline MinerU CLI environment. Read `references/mineru-pdf-bundle.md` before conversion or validation. Before preserving, reusing, human-reviewing, or rebuilding from MinerU intermediates, also read `references/mineru-output-review.md`; keep the original MinerU output immutable and treat reviewed content lists as derived artifacts. Read `references/bundle-source-map-ledger.md` before staged or multi-session ingestion.

For standalone image sources (`.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.gif`, `.tif`, `.tiff`) where the image itself is the source material, create an image Bundle v2 instead of treating the image like a PDF figure:

```text
image_document_bundle/
  manifest.json
  document.md
  outline.json
  images/
  _evidence/
```

Use `python3 "<ingest-skill-root>/scripts/convert_image_with_ocr_bundle.py"`. If OCR text is already available, pass `--ocr-text-file`; if an OCR command is available, pass `--ocr-command`. Without OCR text, still create the bundle as visual evidence with `quality: warn` and `qa_required` sections. Read `references/image-bundle.md` before converting or ingesting standalone image sources.

For Word, PowerPoint, Excel, HTML, CSV, JSON, XML, audio, EPUB, ZIP, URL, or a simple PDF when MinerU is unavailable:

1. Convert the source to Markdown with `python3 "<ingest-skill-root>/scripts/convert_with_markitdown.py"` when available.
2. Save converted Markdown under `10_Raw/converted/`.
3. Keep the original unchanged.
4. Record conversion metadata in the ingest log.

See `references/markitdown.md` and `references/mcp-markitdown.md` when using those paths.

## Layered Bundle Ingestion

Keep the agent-facing path small. Do not recursively scan a bundle.

1. Read `manifest.json` first.
2. Run `python3 "<ingest-skill-root>/scripts/validate_document_bundle.py" <bundle>` before downstream writes.
3. Run `python3 "<ingest-skill-root>/scripts/manage_bundle_ingest.py" init <bundle> --reports-dir <vault>/_system/reports` at the start of every session. This creates or reconciles the source map and section ledger.
4. Use recovery rules when validation returns `fail`; stop at an ingest log or QA report when recovery is unavailable or retry fails.
5. Allow a source map when status is `warn`, but do not promote affected formulas, tables, figures, or parameters as authoritative facts.
6. Select an eligible ledger section. Do not duplicate an `ingested` section or reuse a `stale` section without review.
7. Claim the section as `in_progress` with `--expected-revision` before downstream writes.
8. Read only its ledger `content_ranges` from `document.md`. The enclosing `start_line`/`end_line` scope is context, not an instruction to duplicate nested child content.
9. Follow linked files under `tables/` or `images/` only when the selected section needs them.
10. Record every created output and finish the section as `ingested`, `qa_required`, or `skipped`. Never leave a successful run only in prose logs.
11. Do not read `_evidence/` by default. Open it only for targeted QA of layout, page order, formulas, tables, or extraction disputes.

After ledger initialization, reconciliation, or completed source ingestion, optionally run `python3 "<ingest-skill-root>/scripts/build_section_query_index.py" <vault-root> --bundle <bundle>`. It writes only a disposable, non-authoritative projection under `_system/reports/query-index/`; failure must not change Bundle, ledger, source-map, spec-index, or ingest status. The projection contains no generated section summaries. See `../hermes-obsidian-controlled-query/references/Hierarchical_search.md`.

After a completed source ingest or at the end of a related batch, run `python3 "<ingest-skill-root>/scripts/sync_retrieval_index.py" <vault-root>`. This is the only Skill-side path that may update the configured coarse-recall Provider. It writes a portable status record to `_system/reports/retrieval-index-manifest.json`; Provider databases, model files, caches, and locks remain on the Provider host outside the Vault. Index failure is a retrieval warning and must not change Bundle, ledger, source-map, governed artifact, or ingest completion status. Use `--rebuild` only for an explicit maintenance request, configuration/model incompatibility, or unrecoverable index state. Read `references/retrieval-indexing.md` before configuring local versus HTTP transport.

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

## Evidence Modes

Classify every new or reconciled knowledge card before writing:

- `direct`: a bounded claim set tied to a specific source section, lines, pages, and assets.
- `index`: a navigation or cross-source synthesis artifact whose claims are supported by a structured evidence table and authoritative source maps/section ledgers.
- `relational`: a knowledge-to-knowledge orientation artifact used primarily to express governed relationships; it still needs source provenance for its own statements.

Record the classification as `evidence_mode` in frontmatter. For `index`, also record `evidence_scope: multi-source`, `evidence_coverage: complete | representative`, `evidence_authority: navigation`, and `source_reports`. A representative evidence table must say that it is representative in prose and identify the complete source map/ledger authority. Do not use an ellipsis pseudo-row such as `| … |` as if it were evidence.

Never leave template or generator expressions in governed output. Resolve or remove placeholders such as Python `.join(...)` expressions before completing ingest.

## Query-Derived Writeback

Use this mode when the input is a query-writeback candidate rather than a new external source. This is not new-file ingest: do not copy sources into `10_Raw/`, do not rebuild bundles, and do not infer completion from the query answer.

Treat the candidate as a navigation and triage record only. Re-check the cited source maps, section ledgers, converted `document.md`, tables/images, and original page evidence before writing any durable artifact.

Process candidates with this gate:

1. Confirm the candidate has `user_question`, `candidate_type`, `evidence_level`, `possible_artifact`, and evidence packet paths.
2. Re-open the cited governed artifacts and source evidence. If the evidence cannot be found or no longer supports the answer, write only a QA/gap item or skip.
3. Search existing `30_Cards/`, `40_Concepts/`, `50_Projects/`, `_system/reports/`, and Dataview metadata for coverage and near-duplicates.
4. Classify the query-derived knowledge using broad, non-domain-specific types:
   - `parameter-or-design-value`
   - `review-checklist`
   - `interface-or-handoff`
   - `code-or-principle`
   - `object-or-equipment`
   - `gap-or-conflict`
5. Route conservatively:
   - create/update a `30_Cards/` card only for reusable, evidence-backed knowledge
   - update a spec index when the value is primarily navigational or rule-to-source mapping
   - write a QA/gap item when evidence is missing, conflicting, or extraction-sensitive
   - create a candidate concept review when cross-source reuse is plausible but boundaries are not clear
   - skip when existing artifacts already cover the answer or the value is one-off
6. Record the query candidate path or conversation handoff, source evidence re-check, duplicate check, decision, and skipped alternatives in a writeback log.

Never promote the query answer summary itself into a card, concept, or spec index. Durable artifacts must cite the underlying source PDF/bundle/section/page/asset evidence.

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
9. Reclassify legacy cards under the evidence modes above, remove unresolved generator placeholders, make representative evidence coverage explicit, and run the knowledge-graph relation pass. Perform these repairs only through controlled ingest, never as an incidental query edit.

## Conservative Obsidian Linking

After creating or updating a card, concept, project note, spec index, candidate review, or query-derived writeback log, run a conservative wikilink pass.

Add `[[...]]` links only to existing governed artifacts that are useful navigation targets:

- existing `30_Cards/` cards
- existing `40_Concepts/` concepts with clear boundaries
- existing `50_Projects/` project notes
- relevant spec indexes, source maps, or candidate reviews under `_system/reports/`
- Dataview notes when the artifact is meant to appear in a maintained view

Do not add wikilinks for every repeated noun, equipment name, field name, section title, raw PDF, converted bundle file, table asset, or unapproved concept. Use normal citations and source metadata for evidence paths; use wikilinks for governed knowledge-to-knowledge navigation.

If a useful target does not exist yet, record it under "candidate concept but not created", "possible future card", or a QA/review note instead of creating an empty Obsidian link.

## Knowledge-Graph Relation Pass

Treat governed knowledge relationships as a core ingest output, not optional graph decoration. Concept governance limits new concept pages; it does not prohibit evidence-backed relationships among existing cards, concepts, projects, indexes, and candidate reviews.

After all artifacts for the current source or batch exist, run a second pass across the completed set:

1. Compare each new or updated durable artifact with existing governed artifacts and the other outputs from the same batch.
2. Add typed `[[wikilinks]]` when the relationship is supported by shared source scope, an explicit evidence-table overlap, a direct-versus-index relationship, or a clear semantic boundary.
3. Use concise relationship labels such as `broader-index`, `detail-synthesis`, `direct-evidence-for`, `implementation-case`, `workflow-extension`, `contrast`, or `sibling`.
4. Prefer Card-to-Card and Card-to-index links when concept boundaries are not approved. Do not create a Concept merely to obtain a graph node.
5. When two or more relevant governed knowledge artifacts exist, do not finish with zero links unless the ingest report names the inspected targets and explains why every candidate relationship was rejected.
6. Keep evidence citations as ordinary paths and metadata. A wikilink expresses a governed knowledge relationship, not source proof.
7. Check every emitted link resolves to an existing governed Markdown target.

## Batch Synthesis Phase

After source ingestion for a batch, automatically synthesize when two or more related sources have valid ledgers:

1. Select high-value sections from successful ledgers: `ingested`, non-QA, non-skipped, and citation-ready.
2. Compare related sources for repeated system objects, equipment, operating parameters, design criteria, interfaces, validation rules, and terminology.
3. Generate a small number of high-confidence `30_Cards/` when evidence is clear across one or more sources. Each card must cite source PDF, bundle id, section id, pages or lines, and QA exclusions.
4. Create candidate concept reviews for stable cross-source objects with reusable value. Create actual concept pages only when boundaries are clear and registry checks pass.
5. Do not let bundle-level `warn` block synthesis. Exclude affected QA sections and use only unaffected evidence.
6. If no card or candidate concept is created, justify that with inspected section ids and evidence gaps; do not only say the material is long or engineering-dense.
7. Update existing cards, spec indexes, registry notes, or Dataview metadata when new evidence changes links, sources, relationships, or stale/review-required status.
8. After all batch artifacts are present, run the knowledge-graph relation pass across both new and existing governed artifacts.

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
12. Query-derived writeback decisions when applicable: candidate read, source evidence re-checked, artifact created/updated/skipped, and why.
13. Conservative wikilink pass: existing artifacts linked and candidate links intentionally not created.
14. Batch synthesis decisions: sections compared, cards created or updated, candidate concepts, and rejected candidates.
15. Failed bundle recovery attempts and remaining manual checks.
16. Extraction QA items and recommended next step.
17. Coarse-recall index sync status, Provider/version and fingerprints, or the reason sync was skipped/unavailable.
18. Evidence-mode reconciliation: direct/index/relational classification, coverage declaration, source-report authority, and unresolved placeholders removed.
19. Knowledge-graph relation pass: typed links added, targets verified, or explicit zero-link rejection rationale.
