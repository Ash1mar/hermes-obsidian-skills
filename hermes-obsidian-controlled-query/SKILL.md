---
name: hermes-obsidian-controlled-query
description: Governed querying and retrieval tracing for Hermes + Obsidian vaults. Use when asked to answer, locate, verify, compare, summarize, identify gaps, or audit how an answer was retrieved from an existing governed vault. Supports evidence-first lookup across governed artifacts, reports, ledgers, converted sources, and hierarchical source navigation; writes only non-authoritative query traces by default while preserving the controlled writeback boundary for durable knowledge.
---

# Hermes Obsidian Controlled Query

Answer questions from a governed Hermes + Obsidian vault without polluting the vault. Query is not ingest: default to read-only lookup, evidence selection, source checking, cautious synthesis, and an append-only non-authoritative query trace.

```text
user question
-> vault rules and query type
-> governed layer lookup
-> source-map / ledger navigation
-> targeted converted-source verification
-> answer with evidence quality and gaps
-> optional internal query-writeback candidate
```

## Non-Writing Contract

During a controlled query, do not create, modify, rename, move, or delete governed Vault files. The only default write exception is the current run's non-authoritative trace under `_system/reports/query-traces/`. Skip that trace when the user explicitly requests no logging or the Vault is not writable.

Treat these paths as read-only by default:

- `10_Raw/`
- `10_Raw/converted/`
- `30_Cards/`
- `40_Concepts/`
- `50_Projects/`
- `90_Dataview/`
- `_system/metadata/`
- `_system/prompts/`
- `_system/reports/`

Within `_system/reports/`, write only the query trace exception. Do not edit ingest logs, source maps, spec indexes, ledgers, query indexes, or other reports.

If the answer suggests a durable artifact, do not create it during query. Record only an internal query-writeback candidate when the user or vault policy explicitly allows candidates; otherwise keep the candidate in the current conversation for a possible later ingest handoff. A query trace is not a writeback candidate and must never be promoted or cited as evidence.

## Query Trace

After resolving the Vault and classifying the question, run `scripts/manage_query_trace.py start` with the question, query type, and Hermes session ID when available. Retain its `trace_id` and append an event after every attempted retrieval layer, including zero-hit, skipped, fallback, and failed stages. Record paths, counts, concise selection/exclusion reasons, and evidence checks; never record hidden reasoning, credentials, unrestricted tool output, or long source passages.

Pass `--trace-id <id>` to `scripts/locate_source_sections.py` so actual hierarchical candidates and match data are recorded directly. Finish the trace as `completed`, `failed`, or `incomplete` before returning. Logging errors never block the answer. Read `references/query-tracing.md` for commands, route names, schema, privacy boundary, and Obsidian rendering.

## Minimal Prompt Contract

Users should only need to provide:

1. The vault path or enough context to identify the vault.
2. The question or list of questions.
3. Any explicit scope preference, such as "only use ingested FNP manuals" or "include source-page evidence".

Do not require users to restate this skill's search order, evidence packet fields, QA labels, or writeback restrictions in every prompt. Infer and apply those rules from this skill.

If the user asks for an engineering answer and does not specify an output format, use the full controlled-query answer shape with evidence packets.

## First Reads

At the start of a vault query, read only the minimum governance files needed:

1. `AGENTS.md`
2. `_system/metadata/concept-registry.md` when concepts or concept boundaries matter
3. `_system/prompts/hermes-ingest-rules.md` or query/workflow prompts when present and relevant

Do not recursively load the whole vault. Use `rg` and targeted file reads.

## Query Types

Classify the question before searching:

| Type | Use for | Preferred path |
| --- | --- | --- |
| locating | "Where did we mention X?" | cards/concepts/projects, then reports |
| explanatory | "What is X?" | concepts/cards, then source reports |
| synthesis | "Compare X and Y" | cards/concepts/projects plus selected reports |
| evidence | "Why do we say X?" | reports and converted sources required |
| gap | "Do we already have X?" | governed layers plus reports; answer with missing pieces |

For engineering parameter, formula, table, or figure questions, treat the query as evidence type even if it is phrased as a simple lookup.

## Post-Query Writeback Candidate

Default to no candidate. Create a candidate only when the answer reveals reusable knowledge that is not already covered by a governed artifact, or when the query exposes an evidence gap, QA risk, or source conflict worth later review.

High-value candidate triggers include:

- a source-backed answer with no existing durable card/spec index coverage
- repeated or workflow-relevant parameter, formula, design value, interface, review checklist, validation rule, or code-like requirement
- cross-source synthesis that creates a new useful comparison or boundary
- a gap, conflict, stale conclusion, or QA-sensitive table/figure/formula needed for future work
- a user explicitly asks to log, persist, queue, or later ingest query findings

Do not create a candidate for:

- one-off locating questions
- answers fully covered by an existing card/concept/project/spec index
- weak evidence that only supports speculation
- ordinary chat summaries with no vault evidence
- every user question by default

Candidate question types are broad heuristics, not fixed domain labels:

| Candidate type | Use for | Likely later artifact |
| --- | --- | --- |
| `parameter-or-design-value` | values, formulas, pressures, flow rates, intensities, levels, durations, classifications | parameter card, design check, or QA item |
| `review-checklist` | review points, acceptance checks, equipment checks | checklist card or object index |
| `interface-or-handoff` | inter-discipline inputs/outputs, fields, source boundaries | interface spec index or interface card |
| `code-or-principle` | standard applicability, design principles, rule-to-scenario mapping | code/scenario/spec crosswalk |
| `object-or-equipment` | equipment composition, system objects, reusable object boundaries | object index, equipment card, or candidate concept |
| `gap-or-conflict` | missing evidence, conflicting sources, QA-sensitive evidence | QA item, gap log, or candidate review |

When a candidate is allowed, include only a compact handoff:

```yaml
type: query-writeback-candidate
status: candidate
user_question:
answer_summary:
candidate_type:
evidence_level: clear | source-backed | needs-qa | gap
possible_artifact: none | card | spec-index-update | qa-item | candidate-concept-review | project-note | dataview
why_candidate:
why_not_direct_write:
evidence_packets:
existing_artifacts_checked:
qa_risks:
```

Do not present this candidate in the user-facing answer unless the user asks for writeback reasoning. If persisted, write it only under `_system/reports/query-writeback-candidates/` and treat it as a review queue, not a knowledge artifact.

## Search Order

Use the most governed layer that can answer the question, then descend only as needed:

1. `30_Cards/`, `40_Concepts/`, `50_Projects/`
2. `_system/reports/*.source-map.md`
3. `_system/reports/*.spec-index.md`
4. `_system/reports/*.section-ledger.json`
5. `_system/reports/*controlled-ingest-log.md`
6. `10_Raw/converted/*_document_bundle/document.md`
7. `10_Raw/converted/*_document_bundle/tables/` or `images/` when the cited section requires them
8. Original `10_Raw/` only for source-page verification, extraction disputes, or missing converted evidence

For layered MinerU bundles, prefer `document.md` plus source map/ledger navigation. Open `_evidence/` only for targeted QA of page order, formulas, tables, figures, or extraction disputes.

When governed artifacts do not fully answer the question or source evidence is required, run `scripts/locate_source_sections.py <vault-root> <query> --trace-id <id>` as a parallel candidate locator beside the existing report-navigation search. Merge and deduplicate both candidate sets, then continue through the existing converted-source and page-evidence verification steps. Treat query-index output only as navigation: never quote it or promote it to evidence. If the projection is absent, stale, or invalid, record the fallback and continue with the existing search order without failing the query. Read `references/Hierarchical_search.md` for the design and migration boundary.

## Evidence Quality

Before answering, label the usable evidence:

- `clear`: governed card/concept or pass-quality source section directly supports the point.
- `source-backed`: converted source text supports the point, but no durable card exists yet.
- `needs-qa`: formula, engineering parameter, cross-page table, figure internal, or section marked `qa_required`/bundle warning affects the point.
- `gap`: no adequate governed or source evidence found in the current vault.

Do not promote `needs-qa` evidence into an authoritative reusable fact. State the limitation and what page/table/figure should be checked next.

Read `references/evidence-levels.md` when the question involves formulas, parameters, tables, figures, QA sections, or conflicting sources.

## Answer Evidence Packet

For each substantive conclusion, include an evidence packet with:

1. Document name: the source PDF/manual/report name, not only the converted bundle path.
2. Original PDF page: use source map/ledger page numbers when available; call it "original PDF page" only when the source map ties the section to source pages.
3. Original relevant paragraph: quote or tightly summarize the supporting paragraph from `document.md`; keep quotes short and preserve enough wording for verification.
4. Figure/table location: if a figure or table is used or needed, give its original PDF page and converted asset path such as `tables/*.md`, `*_source.jpg`, or `images/*`. If no figure is involved, state "none found/needed".

If any of these fields cannot be established from the current vault, say so explicitly and mark the evidence `needs-qa` or `gap`.

## Answer Shape

Return concise answers with these parts when the query is non-trivial:

1. Query type
2. Query scope
3. Main hits
4. Answer
5. Evidence packets with document name, original PDF page, original paragraph, and figure/table location
6. Uncertainty / gaps

Do not include a user-facing writeback recommendation by default. If the user asks whether the result should be persisted, summarize the writeback candidate decision in plain language.

For quick locating queries, a shorter answer is acceptable if it still includes file paths and evidence quality.

Read `references/answer-format.md` for the full response contract.

## Writeback Boundary

If a query reveals a reusable result, do not write it automatically. At most, create or retain an internal query-writeback candidate for later controlled ingest:

- create or update a `30_Cards/` knowledge card
- create a candidate concept review
- update a spec index or project note
- record a query log
- run controlled ingest or controlled writeback

Use `hermes-obsidian-controlled-ingest` only when the user explicitly asks to persist, reconcile, or create governed artifacts, or when a later controlled ingest run is explicitly processing the query-writeback candidate queue.

## References

- `references/query-workflow.md`: full workflow, search tactics, and FNP-style engineering query guidance.
- `references/evidence-levels.md`: evidence quality labels and QA restrictions.
- `references/answer-format.md`: standard answer templates.
- `references/query-tracing.md`: incremental trace lifecycle, event schema, privacy boundary, and Obsidian dashboard.
