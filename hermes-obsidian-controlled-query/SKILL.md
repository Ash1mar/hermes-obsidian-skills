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

After `skill_view`, run one request bootstrap instead of searching configuration or reading rules in separate rounds:

```bash
python3 "<query-skill-root>/scripts/query_session.py" bootstrap <vault-root>
```

It returns the exact applicable `AGENTS.md`/`ENVIRONMENT.md` content, routing/provider configuration, inherited session linkage, and deterministic PDF-verification capability. Do not search for those files again during the same request.

For one ordinary question, target three query-session invocations:

```text
begin -> inspect -> finalize
```

Visual verification is opt-in. Add `--verification-required` only when the user or an explicit audit requirement asks for an original-page visual check. A table, formula, engineering parameter, image reference, or Bundle QA flag does not trigger it by itself; ordinary queries trust the Bundle and report concrete QA limitations without opening the original-page visual path. When selected, use exactly one deterministic preparation step after inspect:

```text
begin -> inspect -> verify -> one visual check when ready -> finalize
```

Do not enumerate old traces for a new question, probe stable CLI help, run inline Python, create temporary formatter/helper scripts, or narrate every internal step. Look for an old trace only when the user explicitly asks to resume one.

Treat the compact candidates returned by `begin` as the complete operational input for the first inspection. They are packed within the fixed window to preserve strong-document diversity and add distinct matched question facets. Do not open the full candidate sidecar or trace state to look for a better list. Select the smallest set that jointly covers the requested facets, and inspect all of those candidates in one call; do not select extra candidates merely because they are present. When a later exact registered section is genuinely needed, copy `document_path` verbatim from a returned compact candidate or evidence packet and append `::section-id`; never substitute a document ID, shortened bundle path, or guessed prefix. Do not probe `--help` or read script source to discover selector syntax. If that exact form fails, do not try alternate selector shapes; finalize from the inspected evidence, using `incomplete` when the remaining gap prevents a supported answer.

### 1. Begin

Resolve the Vault, classify the question, and run:

```bash
python3 "<query-skill-root>/scripts/query_session.py" begin \
  <vault-root> "<question>" --query-type <type> [--verification-required]
```

Choose `--verification-required` only from an explicit user/audit requirement, not from question keywords or Bundle content. `query_session.py` deliberately performs no domain-, language-, equipment-, or parameter-specific classification. Pass a Vault root supplied by the user directly; do not search parent directories for another Vault.

`begin` creates the required trace, runs optional coarse recall and hierarchical routing in parallel, fuses candidates, records route timings, and returns at most five compact candidates. The complete fused scope remains in the trace sidecar. An unavailable or disabled Provider is non-blocking.

Hermes session and message IDs are inherited from `HERMES_SESSION_ID` and `HERMES_SESSION_MESSAGE_ID`. Do not invent or manually copy a session ID. Use `--session-id` only outside Hermes when no runtime session context exists.

The retrieval structure remains `optional coarse recall || hierarchical routing`; both branches are navigation-only and query never mutates either Provider or Vault evidence.

### 2. Inspect

Select only the candidates needed to answer and inspect them together:

```bash
python3 "<query-skill-root>/scripts/query_session.py" inspect \
  <vault-root> <trace-id> --candidate 1 --candidate 4
```

`inspect` reads complete section-owned ranges in one batch and resolves related governed outputs, table/image Markdown, optional verification images, manifest, ledger, source-map, original PDF path/pages, QA status, and viewer URL when available. Prefer one inspection call. At most one second inspection is permitted for a real gap, conflict, missed source, or already-known exact section; a third inspection is blocked and requires immediate finalization as completed or incomplete. A `document/path::section-id` selector may name any exact section registered in the query projection, even when it was outside the compact fused top-k; this is the audited route for an already-known section.

Each returned packet has an ASCII `evidence_ref` such as `P1`. The trace stores the corresponding path, version, section, pages, original PDF, and viewer metadata. Retain only the packet reference when synthesizing claims; never copy those provenance fields into finalization input.

If `begin` explicitly marked visual verification as required, run `query_session.py verify <vault-root> <trace-id> --evidence-ref P1` once. Open the returned registered carrier when status is `ready`. When it is `unavailable` or `failed`, stop verification attempts, use `needs-qa`, and copy its `required_unresolved` into the decision. Never try `pdftotext`, inspect Python PDF libraries, probe other binaries, enumerate Bundle files, or re-search converted text as substitutes for that visual check. The packet is an internal verification carrier; the original PDF remains the user-facing source.

### 3. Finalize

After synthesizing claims, run one atomic finalization call with compact decision JSON:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <trace-id> --decision-json '<json-object>'
```

The decision contains only `status`, `evidence_level`, `claims`, `verified_evidence_refs`, `events`, `conclusion`, and `unresolved`. `unresolved_items` is accepted as a compatibility alias. Top-level decision and claim fields remain strict: unknown fields, blank claims, conflicting aliases, invalid references, or an unsupported evidence level for explicitly required but incomplete verification are rejected without changing the in-progress trace. Event standard fields are `stage`, `route`, `status`, `summary`, `evidence_refs`, `inspected_paths`, `hit_count`, `duration_ms`, and `accounting`; unknown event fields are retained under `extensions` and never satisfy a stage, evidence, or verification gate. Events are optional and should be omitted unless they record a real audit or verification fact. A verified reference requires a completed `page-asset-verification` event with `inspected_paths`. The script assigns evidence/claim IDs, inherits all provenance from inspected packets, and verifies the Markdown note exists. Do not create, write, or patch a temporary manifest. Legacy `--manifest-json` and `--manifest` exist only for compatibility/debugging.

Use the minimum sufficient claim set. Every claim must answer a necessary facet of the user's question; merge closely related parameters when the same evidence supports them. Put a scope or evidence boundary in a short qualification on the affected claim instead of creating a separate background or applicability claim, unless the user explicitly asked for that information. Keep only unresolved items that materially change correctness or use of the answer. Write one short conclusion that synthesizes rather than repeats the claims.

Preserve the scope expressed by the inspected evidence when writing the conclusion. If the evidence is narrower than the wording of the question, state that boundary as a concise qualification derived from the evidence itself. Do not launch additional retrieval solely to broaden the scope, and do not encode domain-, system-, standard-, language-, or parameter-specific scope rules in the Skill.

If the first evidence packet is insufficient, run `supplement` once with an explicit gap reason and then run `inspect` again. Only one supplement and two total inspections are permitted per trace. Finalization is blocked until that second inspection records `evidence-gap-review`:

```bash
python3 "<query-skill-root>/scripts/query_session.py" supplement \
  <vault-root> <trace-id> "<focused query>" --reason "<missing evidence>"
```

Read `references/query-workflow.md` for selectors and the finalization decision. Read `references/query-tracing.md` only for trace schema, legacy fallback, grouped questions, or debugging.

If a query-session command fails internally, do not patch the installed Skill, relax Vault path validation, probe implementation alternatives, or continue retrying. Preserve the recorded failure and finalize the trace as `incomplete` with a concrete unresolved item when the failure prevents a supported answer. Skill maintenance belongs in the source repository and a separately tested deployment workflow.

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

- `clear`: pass-quality governed/source or Bundle evidence supports the claim and resolves to an original PDF and page.
- `source-backed`: current converted Bundle/source evidence supports the claim and resolves to an original PDF and page but lacks a durable governed conclusion.
- `needs-qa`: relevant Bundle/control metadata reports `qa_required`, warning-affected, incomplete or ambiguous extraction; sources conflict; or explicitly required verification remains incomplete.
- `gap`: adequate original-PDF evidence is unavailable.

Read `references/evidence-levels.md` for parameters, formulas, tables, figures, conflicts, or QA warnings. Never promote `needs-qa` evidence into an authoritative reusable fact.

### User-facing evidence

For each substantive conclusion provide:

1. original PDF filename and Vault-relative PDF path;
2. original PDF page;
3. relevant passage, quoted or tightly summarized;
4. table/figure number or caption, section, page region, and reliable coordinates when available;
5. evidence quality and unresolved limits.

Never substitute a Bundle, Markdown, source-map, ledger, spec index, trace, or extracted-asset path for the original PDF citation. The Bundle is trusted as the default internal extraction carrier when its quality metadata passes; trusting it does not change the user-facing original-PDF citation. Record source maps, ledgers, `document.md`, tables, extracted images, and page images only in the trace. They are internal retrieval and QA details, not user-facing evidence sources.

Inspect the top-level `answer_contract`. When `viewer_enabled` is true, produce a final `原文定位` list. Use only locator-returned `viewer_url` values for every verified candidate actually used, deduplicate identical URLs, and label each with the original PDF filename and section ID. The URL shape is `doc=<document_id>&section=<section_id>&from=<match_start_line>&to=<match_end_line>`; never invent a document ID or line range. Viewer URLs are navigation aids, not evidence. If no used hit has an eligible URL, state under uncertainty/gaps that source positioning is unavailable; do not allow silent omission of both the links and the unavailable status.

Read `references/answer-format.md` only when a non-trivial answer needs the full response template.

## Multiple questions

Process independently answerable questions strictly one at a time. Generate one request ID, pass the shared `--request-id`, one-based `--question-index`, and total `--question-count` to each `begin`, then inspect, synthesize, finalize, and verify that trace before starting the next question. `begin` rejects a second trace while the request contains an `in_progress` trace, inconsistent counts, duplicate/gapped indices, multiple question marks, or multiple numbered question items. Split the request and retry sequentially when this occurs. Do not use a Hermes session ID as a trace ID, keep two traces open, answer questions concurrently, or create an ad hoc orchestration script. Report each trace separately.

On the final question, pass `--close-request` to `finalize`; it validates the expected count and returns the complete request capsules without a separate `request-summary` round. `request-summary` remains available for later inspection/debugging and rejects unfinished or non-contiguous requests. Carry only the returned deduplicated `sources`, claim `source_ids`, conclusion, and unresolved items into the combined response; do not reload completed evidence packets.

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
- `references/query-performance-optimization.md`: maintainer-only incident history, latency design, benchmarks, and intranet A/B acceptance; read only for performance work or debugging.
- `references/coarse-retrieval.md`: Provider contract and main/intranet boundary.
- `references/Hierarchical_search.md`: projection and hierarchical locator design.
