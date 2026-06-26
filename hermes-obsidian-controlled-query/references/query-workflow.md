# Controlled Query Workflow

Use this reference for rigorous vault queries where a simple card lookup is not enough.

## 1. Establish Scope

Identify:

- vault root
- user question
- query type
- whether the user asked for read-only query or writeback
- whether engineering formulas, parameters, tables, or figures are involved

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

## 7. Recommend Next Action

End with one of:

- no writeback needed
- create/update card
- create candidate concept review
- verify source page/table/figure first
- run controlled ingest for missing source material
- define a query log or Dataview view

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
