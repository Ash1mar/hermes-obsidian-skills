# Query Trace Contract

Query traces audit navigation and timing without becoming evidence.

## Authority and write boundary

- Write only under `_system/reports/query-traces/`.
- Treat Markdown traces and JSON sidecars as disposable, non-authoritative runtime records.
- Never cite a trace, query index, source map, ledger, or conversion carrier as factual evidence.
- Do not record hidden reasoning, credentials, unrestricted tool output, or long source passages.
- A read-only controlled query still creates its trace unless the user explicitly opts out or the Vault is unwritable.
- Logging failure never blocks the answer or permits governed writeback.

## Runtime path

Resolve `<query-skill-root>` from the active Skill loader. On Hermes use `skill_view(name="hermes-obsidian-controlled-query")`, then verify `linked_files.scripts` contains `query_session.py`. Execute the bundled script without loading its source during normal operation.

The fast entry point is `<query-skill-root>/scripts/query_session.py`.

Never search for runtime scripts under the Vault or hard-code `/root/.hermes/skills`.

## Fast lifecycle

The normal lifecycle is handled by:

```text
query_session.py begin
query_session.py inspect
query_session.py finalize
```

The workflow identifier is `query-session/v2` and trace schema is 1.5. A real evidence gap adds `supplement -> inspect`; this is the only supported supplemental evidence path. A query begun with the domain-neutral `--verification-required` policy adds one deterministic `verify` preparation command.

These three calls automatically record:

- query preflight;
- coarse and hierarchical route attempts;
- aggregate parallel scope retrieval;
- candidate-review interval between `begin` and `inspect`;
- document reading;
- table/figure resolution;
- provenance resolution;
- answer-synthesis interval between `inspect` and `finalize`;
- claim–evidence validation;
- query-session duration and command count.

`inspect` also registers compact evidence handles while returning only the source range, necessary QA status, and registered verification carriers. `finalize --decision-json` inherits all provenance from those handles, creates deterministic ASCII evidence/claim IDs, and stores a compact answer capsule with one deduplicated source catalog plus claim `source_ids`.

Before trace creation, `begin` rejects apparent multi-question input unless the caller explicitly records that the subparts share one evidence set. Finalization rejects empty claim text atomically, so completed Claim–Evidence mappings always identify the assertion being supported.

Original-page visual review is an opt-in audit route, not a default requirement for parameters, formulas, tables, or figures. Bundle extraction is trusted when its control metadata passes. When visual review is explicitly selected, carrier preparation is deterministic: `verify` records `verification-readiness` as `ready`, `unavailable`, or `failed` and never probes alternative PDF tools. Include an actual review as a completed `page-asset-verification` event with `evidence_refs` and `inspected_paths` in the finalization decision. Unavailable required verification must remain `needs-qa` with an unresolved item.

Top-level decision and claim fields are strict. Event fields are forward-compatible: the standard fields are `stage`, `route`, `status`, `summary`, `evidence_refs`, `inspected_paths`, `hit_count`, `duration_ms`, and `accounting`; unknown model-supplied event fields are stored under `extensions`. Extensions remain diagnostic and never count as a required stage, evidence mapping, or verification event.

For query-session evidence traces, `completed` requires these stages to exist:

- `candidate-review`;
- `document-reading`;
- `table-figure-resolution`;
- `provenance-resolution`;
- `answer-synthesis`;
- `claim-evidence-mapping`.

A stage may be explicitly `skipped` when genuinely inapplicable. The trace cannot finish with open legacy timers.

## Timing interpretation

Schema 1.5 stores `started_at`, `ended_at`, `duration_ms`, and `accounting` on events. Diagnostic parallel-route and verification-readiness timings are not added to the accounted total; the aggregate scope event is the primary interval. The Markdown note reports:

- query-session duration;
- accounted primary-stage duration;
- unaccounted query-session duration.

The measurement boundary begins when `query_session.py begin` starts and ends when `finalize` begins final persistence. It does not include time before the first tool invocation or after the final tool returns. Hermes session and message IDs are inherited automatically from the terminal environment; correlate both with `agent.log` for true request-received-to-answer-emitted timing and approval waits.

`attempted_routes` includes disabled/unavailable routes. `effective_routes` and the compatibility `retrieval_route` exclude them.

## What to record

Record concise, reviewable operational facts:

- query classification and selected candidates;
- attempted/effective routes and durations;
- inspected governed and conversion-carrier paths;
- source/version/page/table/figure checks;
- rejected candidates with short reasons;
- Evidence records and Claim–Evidence mapping;
- evidence quality, gaps, and QA limitations;
- Hermes session ID.

Record internal verification carriers, including source maps, ledgers, `document.md`, table Markdown, extracted images, and page images, in the trace only.
Verification-carrier paths belong in this trace, not in the user-facing evidence citation.

Evidence and claim records include `recorded_at` in both the sidecar and rendered Markdown.

## Multiple questions

Each independently answerable question receives its own trace. Reuse one request ID, increment `--question-index`, and pass the same `--question-count`. The script rejects a new begin while that request has an in-progress trace, as well as duplicate/gapped indices and conflicting counts. Never reuse a trace ID, keep traces open concurrently, or batch separate questions through an ad hoc script. Each completed trace stores an answer capsule; the last finalize uses `--close-request` to return all capsules without reloading full evidence packets.

Grouped notes live under `_system/reports/query-traces/<request-id>/`; sidecars remain under `_data/`. `Request Summary.md` is navigation only and reports expected/recorded counts, controlled duration, and detected trace overlap. `--close-request` and later `request-summary` reject unfinished/non-contiguous groups. Map every numbered final answer to its trace path or explicit skipped/unavailable status. True user-message-to-final-token time still comes from the correlated Hermes session/message log.

## Legacy fallback

Use the individual trace manager only when `query_session.py` fails or when debugging a partial trace:

```bash
python3 "<query-skill-root>/scripts/manage_query_trace.py" start \
  <vault-root> "<question>" --session-id <id> --query-type <type>

python3 "<query-skill-root>/scripts/manage_query_trace.py" event \
  <vault-root> <trace-id> --stage <stage> --route <route> --summary "<summary>"

python3 "<query-skill-root>/scripts/manage_query_trace.py" evidence \
  <vault-root> <trace-id> --evidence-id E1 --path <vault-relative-path> \
  --document-version <version> --section-id <id> --page <page>

python3 "<query-skill-root>/scripts/manage_query_trace.py" claim \
  <vault-root> <trace-id> --claim-id C1 --text "<claim>" --evidence-id E1

python3 "<query-skill-root>/scripts/manage_query_trace.py" finish \
  <vault-root> <trace-id> --status completed --evidence-level <level> \
  --conclusion "<conclusion>"
```

Legacy `stage-begin`/`stage-end` remain available for debugging, but do not add them to the fast path; the session workflow collects timing automatically.

Always report one compact audit status:

- `trace: <vault-relative Markdown path>`;
- `trace: skipped (explicit user opt-out)`;
- `trace: unavailable (<short reason>)`.
