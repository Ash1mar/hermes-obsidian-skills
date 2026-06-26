---
name: hermes-obsidian-controlled-query
description: Governed read-only querying for Hermes + Obsidian vaults. Use when asked to answer, locate, verify, compare, summarize, or identify gaps from an existing governed vault without ingesting new sources or writing artifacts. Supports evidence-first lookup across 30_Cards, 40_Concepts, 50_Projects, _system/reports source maps/spec indexes/section ledgers/ingest logs, and 10_Raw/converted document bundles, with explicit separation of vault facts, agent inference, evidence gaps, and QA risks.
---

# Hermes Obsidian Controlled Query

Answer questions from a governed Hermes + Obsidian vault without polluting the vault. Query is not ingest: default to read-only lookup, evidence selection, source checking, and cautious synthesis.

```text
user question
-> vault rules and query type
-> governed layer lookup
-> source-map / ledger navigation
-> targeted converted-source verification
-> answer with evidence quality and gaps
-> optional writeback recommendation only
```

## Non-Writing Contract

During a controlled query, do not create, modify, rename, move, or delete vault files unless the user explicitly asks to write, persist, create a card, update a concept, record a query log, or otherwise "沉淀" the result.

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

If the answer suggests a durable artifact, recommend a controlled writeback step instead of doing it.

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
7. Writeback recommendation

For quick locating queries, a shorter answer is acceptable if it still includes file paths and evidence quality.

Read `references/answer-format.md` for the full response contract.

## Writeback Boundary

If a query reveals a reusable result, do not write it automatically. Suggest one of:

- create or update a `30_Cards/` knowledge card
- create a candidate concept review
- update a spec index or project note
- record a query log
- run controlled ingest or controlled writeback

Use `hermes-obsidian-controlled-ingest` only when the user explicitly asks to persist, reconcile, or create governed artifacts.

## References

- `references/query-workflow.md`: full workflow, search tactics, and FNP-style engineering query guidance.
- `references/evidence-levels.md`: evidence quality labels and QA restrictions.
- `references/answer-format.md`: standard answer templates.
