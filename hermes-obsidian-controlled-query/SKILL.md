---
name: hermes-obsidian-controlled-query
description: On Hermes, MUST call skill_view for hermes-obsidian-controlled-query before any governed Vault query; on other runtimes, load this skill's full instructions before acting. Use when asked to answer, locate, verify, compare, summarize, identify gaps, or audit retrieval from an existing governed Obsidian vault. Supports evidence-first lookup, hierarchical source navigation, and non-authoritative query traces while preserving the controlled writeback boundary.
---

# Hermes Obsidian Controlled Query

Answer questions from a governed Hermes + Obsidian vault without polluting governed knowledge. Query is not ingest: keep evidence and knowledge artifacts read-only while writing the required append-only, non-authoritative query trace.

> **Mandatory interpretation:** "read-only query", "只读受控查询", and similar wording mean that governed evidence and knowledge artifacts must not change. They do **not** disable the query trace. Treat trace creation as required operational audit logging, not knowledge writeback. Skip it only for an explicit no-trace request or an unwritable Vault.

## Runtime Skill Boundary

Use `<query-skill-root>` as the runtime-neutral name for the directory containing this active `SKILL.md`. Resolve it once, in this order:

1. Use the active runtime's loader-injected skill directory.
2. On Hermes, use the concrete expanded value of `${HERMES_SKILL_DIR}` or the `skill_dir` returned by `skill_view`.
3. On another runtime, use that runtime's equivalent active-skill directory.

Treat every `scripts/` and `references/` path in this Skill as relative to `<query-skill-root>`, never to the Vault or the shell's current working directory. Do not hard-code an installation directory.

On Hermes, do not act from catalog metadata alone: load the canonical skill with `skill_view(name="hermes-obsidian-controlled-query")` or invoke it through a slash command/bundle before querying. Before running a bundled script, verify it under the resolved `<query-skill-root>`. Never guess `~/.hermes/skills`, a deployment-specific mount, or another conventional installation path; never search for runtime Skill files under `<vault>/_system/skills`, `<vault>/_system/templates`, or `<vault>/scripts`; and never create replacement Skill scripts inside the Vault.

Do not announce that scripts are uninstalled merely because a guessed path or terminal sandbox cannot see them. On Hermes, first inspect the canonical `skill_view` result and its `linked_files.scripts`; distinguish an incomplete Skill package from a host-to-sandbox mount failure. If the active loader confirms that a required file is absent, report the missing runtime resource and continue with the documented non-blocking fallback where one exists.

```text
user question
-> vault rules and query type
-> start query trace (default required)
-> governed layer lookup
-> source-map / ledger navigation
-> targeted converted-source verification
-> finish query trace
-> answer with evidence quality and gaps
-> optional internal query-writeback candidate
```

## Non-Writing Contract

During a controlled query, do not create, modify, rename, move, or delete governed Vault files. The current run's non-authoritative trace under `_system/reports/query-traces/` is a required operational write, not an optional knowledge write. Skip it only when the user explicitly says not to record/write a query trace (or equivalent), or when the Vault is actually not writable. A generic request for "read-only", "controlled query", "do not modify knowledge", or "不要沉淀" is not a trace opt-out.

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

After resolving the Vault and classifying the question, but before searching governed artifacts, run `<query-skill-root>/scripts/manage_query_trace.py start` with the question, query type, and Hermes session ID when available. Do not postpone this until the end and do not infer an opt-out from read-only wording. Retain its `trace_id` and append an event after every attempted retrieval layer, including zero-hit, skipped, fallback, and failed stages. Record paths, counts, concise selection/exclusion reasons, and evidence checks; never record hidden reasoning, credentials, unrestricted tool output, or long source passages.

Record source maps, ledgers, `document.md`, table Markdown, extracted images, page images, and other conversion or verification carrier paths in the trace only. They are internal retrieval and QA details, not user-facing evidence sources. Do not expose those carrier paths in the answer unless the user explicitly asks for retrieval debugging or Vault maintenance details.

Pass `--trace-id <id>` to `<query-skill-root>/scripts/locate_source_sections.py` so actual hierarchical candidates and match data are recorded directly. Before returning, finish the trace as `completed`, `failed`, or `incomplete` and verify that the returned Markdown trace path exists. Never claim that a trace was written without this check. Logging errors never block the answer, but report `trace: skipped`, `trace: unavailable`, or the created trace path in the final answer. Read `<query-skill-root>/references/query-tracing.md` for commands, route names, schema, privacy boundary, and Obsidian rendering.

## Multiple Questions

When one user message contains multiple independently answerable questions, generate one request ID for that message, preserve the user's order, and complete them strictly one at a time. Treat each question as its own controlled query: classify it, start its own trace with the shared `--request-id` and its one-based `--question-index`, perform retrieval and source verification, synthesize its answer, finish and verify its trace, and only then start the next question. The trace manager places the visible trace notes in one request folder and maintains `Request Summary.md`; this grouping does not merge their trace lifecycles. Do not keep more than one question trace open, combine independent questions into one trace merely because they share a prompt or Hermes session, or run the questions concurrently.

Do not use a Hermes session ID as a trace ID. Retain the unique `trace_id` returned by each `start` call and pass it to that question's locator, events, and `finish` call. Do not create an ad hoc Python, shell, or other orchestration script to batch the questions. Invoke the existing Skill scripts separately for each question. The documented parallel candidate locator may operate only within the current question; it does not authorize parallel question answering. If processing stops or a question fails after its trace starts, finish that trace as `failed` or `incomplete` when possible before continuing. Report the request folder and a separate trace path or trace status for every question in the final response.

Treat tightly coupled subparts that require one shared body of evidence to support a single conclusion as one composite question and one trace. Otherwise prefer separate questions and separate traces.

## Minimal Prompt Contract

Treat an explicit request to use `hermes-obsidian-controlled-query` as sufficient activation of the complete controlled-query contract. The user does not need to add "read-only", "controlled", "create a trace", or equivalent operational wording. Absence of those phrases never relaxes governed-artifact protection and never disables the default query trace.

Users should only need to provide:

1. The vault path or enough context to identify the vault.
2. The question or list of questions.
3. Any explicit scope preference, such as "only use ingested FNP manuals" or "include source-page evidence".

Do not require users to restate read-only boundaries, trace requirements, search order, evidence packet fields, QA labels, or writeback restrictions in every prompt. Infer and apply those rules from this skill.

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

When governed artifacts do not fully answer the question or source evidence is required, run `<query-skill-root>/scripts/locate_source_sections.py <vault-root> <query> --trace-id <id>` as a parallel candidate locator beside the existing report-navigation search. Merge and deduplicate both candidate sets, then continue through the existing converted-source and page-evidence verification steps. Treat query-index output only as navigation: never quote it or promote it to evidence. If the projection is absent, stale, or invalid, record the fallback and continue with the existing search order without failing the query. Read `<query-skill-root>/references/Hierarchical_search.md` for the design and migration boundary.

## Evidence Quality

Before answering, label the usable evidence:

- `clear`: a governed artifact with `evidence_mode: direct` or a pass-quality source section directly supports the point, and the current query can resolve its provenance.
- `source-backed`: converted source text supports the point, but no durable card exists yet.
- `needs-qa`: formula, engineering parameter, cross-page table, figure internal, or section marked `qa_required`/bundle warning affects the point.
- `gap`: no adequate governed or source evidence found in the current vault.

Treat `evidence_mode: index` as governed navigation: use its evidence table to select documents and sections, but do not promote section-title or coverage evidence into a detailed factual claim. Treat `evidence_mode: relational` and standalone `[[wikilinks]]` as query expansion and graph navigation, never as source proof. When legacy artifacts have no `evidence_mode`, classify their provenance before assigning an evidence level.

Do not promote `needs-qa` evidence into an authoritative reusable fact. State the limitation and what page/table/figure should be checked next.

Read `references/evidence-levels.md` when the question involves formulas, parameters, tables, figures, QA sections, or conflicting sources.

## Answer Evidence Packet

For each substantive conclusion, include an evidence packet with:

1. Original PDF: give the source PDF filename and its original PDF path when resolvable. Never substitute a Bundle, Markdown, source-map, ledger, or extracted-asset path.
2. Original PDF page: use source map/ledger page mappings only when they tie the checked section to the source PDF.
3. Relevant passage: quote or tightly summarize the passage anchored to that original PDF page. Converted text may assist internal verification, but do not identify the converted file as the source. Use a direct quote only when extraction quality supports it; otherwise summarize and mark the limitation.
4. Figure/image/table location: if a figure, page image, or table is used or needed, give its original PDF page, number or caption, containing section, and specific page region such as upper/lower page or left/right column. Include a bounding box or equivalent region only when the Vault provides a reliable mapping. Do not return a converted table/image/page-image path. If none is involved, state "none found/needed".

If the original PDF identity, page mapping, or relevant passage cannot be established from the current Vault, do not fall back to citing `document.md` or another conversion carrier. Say which original-PDF field is unresolved and mark the evidence `needs-qa` or `gap`.

## Answer Shape

Return concise answers with these parts when the query is non-trivial:

1. Query type
2. Original-PDF query scope
3. Main original-PDF hits
4. Answer
5. Evidence packets with original PDF identity/path, original PDF page, relevant passage, and original-PDF figure/image/table location
6. Uncertainty / gaps

Do not include a user-facing writeback recommendation by default. If the user asks whether the result should be persisted, summarize the writeback candidate decision in plain language.

For quick locating queries, a shorter answer is acceptable if it still identifies the original PDF, original PDF page, and evidence quality. Do not replace them with internal Vault conversion paths.

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
