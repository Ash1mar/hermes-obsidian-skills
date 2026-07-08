# Controlled Query Workflow

Use this reference for rigorous vault queries where a simple card lookup is not enough.

## 1. Establish Scope

Identify:

- vault root
- user question
- query type
- whether the user asked for read-only query or writeback
- whether engineering formulas, parameters, tables, or figures are involved
- whether a query-writeback candidate is allowed by the user or vault policy

If the user did not explicitly ask to write, treat the run as read-only.

## 2. Read Governance

Read:

- `AGENTS.md`
- `_system/metadata/concept-registry.md` when concept boundaries matter
- relevant `_system/prompts/` files only if they govern query or answer behavior

Avoid loading ingest prompts unless the query asks about ingestion behavior or writeback.

## 3. Search Governed Layers

Start with:

```bash
rg -n "keyword|synonym|abbreviation" 30_Cards 40_Concepts 50_Projects
```

Use this layer for:

- existing conclusions
- reusable cards
- concept definitions
- project context
- prior synthesis

If this layer answers the question clearly, cite it and stop unless the user asked for source proof.

## 4. Search Control Reports

Search `_system/reports/` for navigation and quality state:

- source maps identify sections, pages, status, quality, and outputs
- section ledgers identify line ranges, status, and stale/QA states
- spec indexes identify chapter objects and source navigation
- ingest logs explain decisions and QA exclusions

Use reports to decide which converted source files and line ranges to open. Do not treat spec indexes alone as final engineering facts when they explicitly say they are navigational.

## 5. Verify Converted Sources

For evidence queries, open targeted converted sources:

- `10_Raw/converted/*_document_bundle/document.md`
- selected `tables/*.md`
- selected `images/*` when the figure itself is evidence

Prefer section ranges from the ledger/source map. Do not read every converted bundle by default.

For engineering values, formulas, table rows, or figure internals, compare the text with source map quality. If extraction quality is uncertain, state that the source page or image must be checked.

When forming an answer, build an evidence packet for each conclusion:

- document name from the source map, spec index, bundle manifest, or raw PDF name
- original PDF page from the source map/ledger `Pages` field
- relevant original paragraph from `document.md` or the selected table Markdown
- figure/table location from the source page plus `images/`, `tables/*.md`, or `*_source.jpg`

If only converted line numbers are available, do not imply they are original page numbers. Report them as converted-source lines and mark the page requirement as unresolved.

## 6. Synthesize

Separate:

- what the vault explicitly says
- what the converted source explicitly says
- what the agent infers from those sources
- what remains missing or unsafe to conclude

When sources conflict, report the conflict and list the paths instead of forcing a single answer.

## 7. Decide Candidate Status

Default to no writeback candidate. Create an internal candidate only when the result is both reusable and evidence-grounded, or when it exposes a material gap, conflict, stale artifact, or QA risk.

Strong triggers:

- a `source-backed` answer that lacks a durable card/spec index
- repeated or workflow-relevant parameters, formulas, design values, classifications, interfaces, review checks, validation rules, or code-like requirements
- cross-source comparison that creates a useful reusable boundary or mapping
- a gap/conflict/QA issue that future users are likely to ask again

Do not create candidates for one-off locating tasks, fully covered existing artifacts, weak speculation, or ordinary chat summaries.

When a candidate is useful but logging is not allowed, retain it only in the current conversation and mention it only if the user asks about persistence. When logging is allowed, write the compact candidate under `_system/reports/query-writeback-candidates/`.

Candidate outcomes:

- no writeback needed
- candidate for create/update card
- candidate for candidate concept review
- candidate for spec index/project/Dataview update
- candidate for source page/table/figure QA
- candidate for controlled ingest of missing source material
- gap/conflict candidate

Never perform the writeback without explicit user instruction.

## Engineering Query Notes

Treat FNP/manual questions as evidence queries when they ask for:

- design parameters
- nozzle/sprinkler values
- flow or hydraulic formulas
- seismic or QA classification
- fire hazard classes
- room-level equipment lists
- table rows
- figure details

For these, report source section/page and QA state. A `warn` bundle does not block use, but formulas, tables, figures, and parameters affected by warnings remain `needs-qa` until checked.

## Query-Derived Ingest Handoff

When a later ingest run processes a query-writeback candidate, it must use the candidate only as a navigation and decision aid. The source of truth remains the cited governed artifacts, source maps, section ledgers, converted sources, tables/images, and original page evidence.

The ingest run should re-check:

- whether the cited evidence still exists and has the same quality/status
- whether existing cards, concepts, projects, spec indexes, candidate reviews, or Dataview notes already cover the result
- whether QA-sensitive formulas, tables, figures, or engineering parameters have been verified enough to become reusable facts
- whether the proper result is a durable artifact, QA/gap item, candidate concept review, or skip
