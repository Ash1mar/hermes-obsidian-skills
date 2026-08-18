---
name: hermes-obsidian-controlled-query
description: 受控查询 / Controlled Query：当用户说“请使用 query 回答”“请使用query回答”“使用 query 查询”或“用 query 查 Vault”时，必须选择 hermes-obsidian-controlled-query。也用于从当前、指定或受治理的 Obsidian Vault 回答、检索、定位、核验、比较、总结业务、工程系统、设备、规范、设计参数或技术要求。领域词由部署配置扩展。On Hermes, MUST call skill_view for hermes-obsidian-controlled-query, resolve its skill_dir, and verify linked_files.scripts includes query_session.py; execute bundled scripts without loading their source unless a script fails or is being debugged. On other runtimes, load this full skill first.
---

# Hermes Obsidian Controlled Query

Answer from a governed Vault with original-PDF evidence while keeping governed knowledge read-only. Write only the required non-authoritative query trace unless the user explicitly opts out or the Vault is unwritable.

## Intranet deployment

On the `intranet` branch, read `config/intranet.json` and use its `vault_path` as the governed Vault root. The current deployment uses `/opt/data/phq/testVault`. If the server path changes, update the config; do not switch Vaults from prompt wording.

`hermes_skills_root` is the parent containing all Skills, not `<query-skill-root>`. Prefer the loader-returned Skill directory; use `/opt/data/skills/hermes-obsidian-controlled-query/` only as the configured deployment fallback and consistency check. Report a loader/config mismatch instead of searching the Vault.

Treat `domain_query_terms` as routing hints, never evidence. When `viewer_base_url` is configured, preserve the locator-returned viewer URLs for the final `原文定位` section.

## Runtime boundary

Resolve `<query-skill-root>` from the active loader:

1. Use the loader-injected skill directory.
2. On Hermes, use `${HERMES_SKILL_DIR}` or `skill_dir` returned by `skill_view`.
3. Verify `linked_files.scripts` contains `query_session.py` and the other bundled entry points.

Run Python entry points as `python3 "<query-skill-root>/scripts/<script>.py"`. Never resolve scripts relative to the Vault, parent skill catalog, or shell working directory. Do not search `<vault>/_system/skills`, `<vault>/_system/templates`, or `<vault>/scripts`. Do not hard-code an installation directory.

Do not read bundled script source during a normal query. Read it only after a concrete failure or when modifying/debugging the Skill. Do not announce that scripts are missing until `skill_view` confirms the active package is incomplete rather than merely unmounted in a shell sandbox.

Read `config/domain-routing.json` or `config/intranet.json` when present. Treat configured domain terms as routing hints, never evidence. The retrieval Provider configuration remains deployment-specific; do not invent an intranet endpoint.

## Fast path

For one ordinary question, target three script invocations:

```text
begin -> inspect -> optional original-page visual check -> finalize
```

Do not enumerate old traces for a new question, probe stable CLI help, run inline Python, create temporary formatter/helper scripts, or narrate every internal step. Look for an old trace only when the user explicitly asks to resume one.

### 1. Begin

Resolve the Vault, classify the question, and run:

```bash
python3 "<query-skill-root>/scripts/query_session.py" begin \
  <vault-root> "<question>" --query-type <type>
```

`begin` creates the required trace, runs optional coarse recall and hierarchical routing in parallel, fuses candidates, records route timings, and returns at most five compact candidates. The complete fused scope remains in the trace sidecar. An unavailable or disabled Provider is non-blocking.

Hermes session and message IDs are inherited from `HERMES_SESSION_ID` and `HERMES_SESSION_MESSAGE_ID`. Do not invent or manually copy a session ID. Use `--session-id` only outside Hermes when no runtime session context exists.

The retrieval structure remains `optional coarse recall || hierarchical routing`; both branches are navigation-only and query never mutates either Provider or Vault evidence.

### 2. Inspect

Select only the candidates needed to answer and inspect them together:

```bash
python3 "<query-skill-root>/scripts/query_session.py" inspect \
  <vault-root> <trace-id> --candidate 1 --candidate 4
```

`inspect` reads complete section-owned ranges in one batch and resolves related governed outputs, table/image Markdown, verification images, manifest, ledger, source-map, original PDF path/pages, QA status, and viewer URL when available. Prefer one inspection call. Use a second only for a real gap, conflict, or missed source.

Each returned packet has an ASCII `evidence_ref` such as `P1`. The trace stores the corresponding path, version, section, pages, original PDF, and viewer metadata. Retain only the packet reference when synthesizing claims; never copy those provenance fields into finalization input.

For engineering values, formulas, tables, or figure internals, open the returned original-page or evidence image and verify the relevant row/region. The packet is an internal verification carrier; the original PDF remains the user-facing source.

### 3. Finalize

After synthesizing claims, run one atomic finalization call with compact decision JSON:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <trace-id> --decision-json '<json-object>'
```

The decision contains `claims` with non-empty `text` and `evidence_refs`, plus evidence level, conclusion, unresolved items, and optional verification events. `claim`, `statement`, and `claim_text` are accepted as compatibility aliases for `text`; finalization rejects a claim that remains blank without changing the in-progress trace. The script assigns evidence/claim IDs, inherits all provenance from inspected packets, and verifies the Markdown note exists. Do not create, write, or patch a temporary manifest. Legacy `--manifest-json` and `--manifest` exist only for compatibility/debugging.

If the first evidence packet is insufficient, run `supplement` with an explicit gap reason and then run `inspect` again. Finalization is blocked until that second inspection records `evidence-gap-review`:

```bash
python3 "<query-skill-root>/scripts/query_session.py" supplement \
  <vault-root> <trace-id> "<focused query>" --reason "<missing evidence>"
```

Read `references/query-workflow.md` for selectors and the finalization decision. Read `references/query-tracing.md` only for trace schema, legacy fallback, grouped questions, or debugging.

## Query rules

### Non-writing contract

Treat `10_Raw/`, `30_Cards/`, `40_Concepts/`, `50_Projects/`, `90_Dataview/`, and `_system/` as read-only except the current trace under `_system/reports/query-traces/`. “Read-only query” protects governed artifacts; it does **not** disable the trace. Skip the trace only for an explicit no-trace request or an unwritable Vault.

Never rebuild or sync a retrieval Provider during query. Provider indexes, source maps, ledgers, Bundles, spec indexes, and traces are navigation or verification carriers, not answer evidence.

### Minimal first reads

Read only:

1. Vault `AGENTS.md`;
2. concept registry when concept boundaries matter;
3. a query-specific governance prompt when relevant.

Do not recursively load the Vault or ingest prompts.

### Query types

| Type | Use |
| --- | --- |
| `locating` | Find where something is mentioned |
| `explanatory` | Explain one governed concept |
| `synthesis` | Compare or combine sources |
| `evidence` | Verify a claim, parameter, formula, table, or figure |
| `gap` | Determine whether adequate evidence exists |

Treat engineering parameters, formulas, tables, and figures as `evidence` even when phrased as simple lookups.

### Search and verification

Consume the fused union. Inspect retained `30_Cards/`, `40_Concepts/`, and `50_Projects/` material first when the evidence packet includes it. Use supplemental scoped exact/lexical search only when the packet is insufficient. Broaden beyond the fused scope only for gap, completeness, conflict, or audit questions.

This is governed-layer-first traditional search after candidate fusion. Query must never run Provider `sync` or rebuild operations. extraction QA labels are verification metadata, not relevance boosts or penalties.

Use source maps and ledgers to resolve current/stale status, complete ranges, source identity, pages, hashes, and QA. Treat `evidence_mode: index` as navigation, `evidence_mode: relational` and standalone wikilinks as expansion, and neither as factual proof. Evidence discovered through other navigation tools must still enter a recorded `inspect` packet before it may be cited.

### Evidence quality

- `clear`: pass-quality governed/source evidence resolves to an original PDF and page.
- `source-backed`: checked source evidence resolves to an original PDF and page but lacks a durable governed conclusion.
- `needs-qa`: the claim depends on formula OCR, engineering values, table/figure internals, cross-page structure, or a warning/QA section.
- `gap`: adequate original-PDF evidence is unavailable.

Read `references/evidence-levels.md` for parameters, formulas, tables, figures, conflicts, or QA warnings. Never promote `needs-qa` evidence into an authoritative reusable fact.

### User-facing evidence

For each substantive conclusion provide:

1. original PDF filename and Vault-relative PDF path;
2. original PDF page;
3. relevant passage, quoted or tightly summarized;
4. table/figure number or caption, section, page region, and reliable coordinates when available;
5. evidence quality and unresolved limits.

Never substitute a Bundle, Markdown, source-map, ledger, spec index, trace, or extracted-asset path for the original PDF citation. Record source maps, ledgers, `document.md`, tables, extracted images, and page images only in the trace. They are internal retrieval and QA details, not user-facing evidence sources.

Inspect the top-level `answer_contract`. When `viewer_enabled` is true, produce a final `原文定位` list. Use only locator-returned `viewer_url` values for every verified candidate actually used, deduplicate identical URLs, and label each with the original PDF filename and section ID. The URL shape is `doc=<document_id>&section=<section_id>&from=<match_start_line>&to=<match_end_line>`; never invent a document ID or line range. Viewer URLs are navigation aids, not evidence. If no used hit has an eligible URL, state under uncertainty/gaps that source positioning is unavailable; do not allow silent omission of both the links and the unavailable status.

Read `references/answer-format.md` only when a non-trivial answer needs the full response template.

## Multiple questions

Process independently answerable questions strictly one at a time. Generate one request ID, pass the shared `--request-id` and one-based `--question-index` to each `begin`, then inspect, synthesize, finalize, and verify that trace before starting the next question. `begin` rejects multiple question marks or multiple numbered question items before creating a trace. Split the request and retry sequentially when this occurs. Do not use a Hermes session ID as a trace ID, keep two traces open, answer questions concurrently, or create an ad hoc orchestration script. Report each trace separately.

After all questions finish, call `query_session.py request-summary <vault-root> <request-id>` once. Carry only its answer capsules into the combined response; do not reload completed evidence packets.

Use one trace only for tightly coupled subparts requiring the same evidence set. In that exceptional case pass both `--coupled` and a concrete `--coupled-reason`; the reason is stored in trace state for audit.

## Failure and legacy fallback

If `query_session.py` fails, keep the trace failure non-blocking where possible and use the existing individual scripts as a recorded fallback:

- `manage_query_trace.py`
- `retrieve_query_scope.py`
- `locate_source_sections.py`
- `retrieve_candidates.py`

Finish an opened trace as `failed` or `incomplete` when possible. Never claim a trace was written without checking the returned note path.

## Writeback boundary

Do not create or update governed artifacts during query. Retain a compact internal writeback candidate only when the result exposes reusable knowledge, a material gap/conflict, stale coverage, or a QA risk. Use `hermes-obsidian-controlled-ingest` only after an explicit persistence or reconciliation request.

## References

- `references/query-workflow.md`: fast-path commands, decision schema, evidence-packet use, and fallback search.
- `references/evidence-levels.md`: evidence and QA restrictions.
- `references/answer-format.md`: full user-facing response template.
- `references/query-tracing.md`: trace schema, timing, grouped questions, and legacy commands.
- `references/query-performance-optimization.md`: latency sources, three-call design, benchmarks, and intranet A/B acceptance.
- `references/coarse-retrieval.md`: Provider contract and main/intranet boundary.
- `references/Hierarchical_search.md`: projection and hierarchical locator design.
