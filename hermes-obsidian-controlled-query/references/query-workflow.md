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

## 3. Recall and Fuse Candidate Scope

For ordinary semantic, explanatory, synthesis, and evidence questions, run:

```bash
python3 "<query-skill-root>/scripts/retrieve_query_scope.py" \
  <vault-root> "<query>" --trace-id <trace-id>
```

The script runs optional qmd-like-rag recall and hierarchical document/section location concurrently. It then:

- keeps an unavailable Provider non-blocking;
- expands Provider chunks to complete projected/ledger-owned sections;
- takes the union rather than the intersection;
- merges duplicate document/section and overlapping same-title ranges;
- preserves route-specific raw scores and ranks without adding incomparable scores;
- calculates a fusion RRF score for ordering;
- records retained candidates and duplicate rejection reasons.

For exact identifiers, filenames, clauses, or verbatim phrases, a direct traditional/hierarchical route is allowed when cheaper. Record the skipped parallel route and reason. For gap, completeness, and audit questions, broaden beyond fused top-k scope.

## 4. Run Governed-First Traditional Search

After fusion, inspect retained candidates under `30_Cards/`, `40_Concepts/`, and `50_Projects/` first. Supplement them with exact governed-layer search when needed:

```bash
rg -n "keyword|synonym|abbreviation" 30_Cards 40_Concepts 50_Projects
```

Use governed artifacts for existing conclusions, concept/project boundaries, version applicability, and precise evidence pointers. When one resolves the current source, version, section, original page, and passage, follow it directly to targeted source verification and record broad lexical search as skipped.

When governed evidence is insufficient, run exact/lexical search for identifiers, source filenames, clauses, verbatim phrases, values, units, and synonyms within the fused candidate scope. Do not scan the whole Vault by default. Widen deliberately for gaps, completeness, audit, or a weak/empty fused result.

## 5. Resolve Control Metadata and Verify Evidence

Use `_system/reports/` after candidate selection:

- source maps resolve source PDF identity, pages, quality, and outputs;
- section ledgers resolve complete owned ranges, status, revisions, hashes, and stale/QA state;
- spec indexes provide governed navigation and coverage, not final factual authority;
- ingest logs explain exclusions and conversion decisions when QA requires them.

For evidence queries, open targeted converted sources:

- `10_Raw/converted/*_document_bundle/document.md`
- selected `tables/*.md`
- selected `images/*` when the figure itself is evidence

Prefer section ranges from the ledger/source map. Do not read every converted bundle by default.

For engineering values, formulas, table rows, or figure internals, compare the text with source map quality. If extraction quality is uncertain, state that the source page or image must be checked.

Record each accepted source as an Evidence record with document version, section, original page, block ID, and original table/image verification status. Link verification events to evidence IDs and maintain a Claim–Evidence mapping from every final claim to those IDs. Accepted counts and paths are derived from Evidence records. Do not return conversion or verification carrier paths in the user-facing answer unless the user explicitly asks for retrieval debugging or Vault maintenance details.

Use monotonic stage timers for document reading, table/figure verification, and answer synthesis. Retrieval and fusion scripts record their own monotonic durations.

When forming an answer, build an evidence packet for each conclusion:

- original PDF filename and original PDF path resolved through the source map, ledger, Bundle manifest, or `10_Raw/`
- original PDF page from a verified source-map/ledger page mapping
- relevant passage anchored to that original PDF page; use converted text internally, but present the passage as PDF evidence rather than as a Markdown citation
- figure/image/table location as original PDF page plus number or caption, containing section, and page region; include a bounding box only when reliably mapped to the original PDF

If only converted paths or line numbers are available, do not return them as a substitute citation. Mark the original PDF identity or page as unresolved and label the evidence `needs-qa` or `gap`.

continue until the original PDF identity, original PDF page, and relevant passage are resolved; otherwise preserve the unresolved state in both the Evidence record and final answer.

## 6. Synthesize

Separate:

- what the vault explicitly says
- what the original PDF evidence supports
- what the agent infers from those sources
- what remains missing or unsafe to conclude

When sources conflict, report the conflict using original PDF identities and pages instead of conversion paths, and do not force a single answer.

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
