# Query Trace Contract

Use query traces to audit retrieval navigation without turning runtime history into source evidence.

## Authority and write boundary

- Write only under `_system/reports/query-traces/`.
- Treat Markdown traces and JSON sidecars as non-authoritative, disposable runtime records.
- Never cite a query trace as evidence and never retrieve it as a governed answer source.
- Do not copy long source passages, credentials, hidden prompts, or unrestricted tool output into a trace.
- Keep the rest of the Vault read-only unless the user separately authorizes controlled writeback.
- Logging failure must not block or weaken the answer. Mention the failure in the answer only when auditability was explicitly requested.

The default is one trace per controlled query. For trace purposes, each independently answerable question is a controlled query even when several questions arrive in one user message. This is a required operational side effect of the Query Skill, including when the user calls the query "read-only" or asks not to modify/沉淀 governed knowledge. Those phrases protect knowledge artifacts; they do not disable runtime audit logging. Skip the trace only when the user explicitly requests no query trace/log, or the Vault cannot be written.

## Runtime path

Run the trace manager from the active Query Skill installation, not from the Vault. Resolve `<query-skill-root>` from the location of the active `SKILL.md` supplied by the runtime's Skill loader, then substitute that absolute directory in the commands below:

```bash
test -f "<query-skill-root>/scripts/manage_query_trace.py"
```

Do not hard-code or guess an installation directory. Do not probe `<vault>/_system/skills`, `<vault>/_system/templates`, or `<vault>/scripts` for these runtime files.

## Lifecycle

Start immediately after resolving the Vault, query type, and Hermes session ID, before the first governed-artifact search:

```bash
python3 "<query-skill-root>/scripts/manage_query_trace.py" start <vault-root> "<question>" \
  --session-id <hermes-session-id> --query-type <type>
```

Retain the returned `trace_id`. Append an event after each attempted retrieval layer, including zero-hit and failed stages:

```bash
python3 "<query-skill-root>/scripts/manage_query_trace.py" event <vault-root> <trace-id> \
  --stage governed-artifact-lookup --route governed-artifacts \
  --hit-count 3 --accepted-count 1 \
  --summary "Cards and concepts checked; one card retained for source follow-up." \
  --inspected-path 30_Cards/example.md \
  --accepted-path 30_Cards/example.md \
  --rejected "40_Concepts/example.md::definition only; no parameter evidence"
```

Use stable route names where applicable:

- `governed-artifacts`
- `report-navigation`
- `hierarchical-search`
- `converted-source`
- `page-asset-verification`
- `answer-synthesis`

Pass `--trace-id` to `locate_source_sections.py`; it records the actual hierarchical candidates, scores, match terms, paths, and errors directly.

Finish before returning the answer:

```bash
python3 "<query-skill-root>/scripts/manage_query_trace.py" finish <vault-root> <trace-id> \
  --status completed --evidence-level source-backed \
  --conclusion "Short statement of what the checked evidence supports." \
  --unresolved "Table image still requires visual QA."
```

If execution stops after a trace starts, finish it as `failed` or `incomplete` when possible. Because every event is persisted immediately, a crash still leaves a readable partial trace. Before answering, check that the Markdown path returned by `finish` exists; never substitute a prose reconstruction for a missing trace.

## Multiple-question sequencing

Process independently answerable questions in the user's order with no overlap:

```text
question 1: classify -> start trace 1 -> retrieve -> verify -> synthesize -> finish and verify trace 1
question 2: classify -> start trace 2 -> retrieve -> verify -> synthesize -> finish and verify trace 2
...
```

- Start the next question only after the previous trace is `completed`, `failed`, or `incomplete`, or after recording that its trace was explicitly skipped or unavailable.
- Reuse the Hermes session ID for correlation when appropriate, but never reuse a `trace_id` across independent questions.
- Do not combine independent questions into one trace solely because they share a prompt, source document, or session.
- Do not create an ad hoc Python, shell, or other batch-orchestration script and do not execute separate questions concurrently.
- Keep documented candidate-recall parallelism inside the active question and attach every locator call to that question's trace.
- If one question fails, close its trace when possible and keep its failure, evidence, and uncertainty separate from later questions.
- Use one trace for tightly coupled subparts only when they require one shared evidence set to support one composite conclusion.

In the final response, map each numbered answer to its own trace path or to its own `skipped` or `unavailable` status.

Always include a compact audit status in the final answer:

- `trace: <vault-relative Markdown path>` when written;
- `trace: skipped (explicit user opt-out)` when explicitly disabled;
- `trace: unavailable (<short reason>)` when the Vault or logger could not be written.

## What to record

Record:

- query classification and scope;
- searched layers and paths;
- hit and accepted counts;
- hierarchical candidate scores and matched terms;
- source/page/table/figure verification decisions;
- accepted and rejected candidates with concise reasons;
- evidence level, short conclusion, gaps, and QA needs;
- Hermes session ID for correlation with `/root/.hermes/logs/agent.log`.

Do not record chain-of-thought or hidden reasoning. Record short operational decisions that a reviewer can verify, such as "rejected because section status is qa_required".

## Obsidian view

The trace manager creates `Query Trace Dashboard.md` once. Its Dataview table lists status, query type, route, hierarchical usage, and evidence level. Each trace note uses folded Obsidian callouts and links existing inspected Vault paths. The `_data/` JSON sidecars support deterministic incremental updates; ordinary readers can ignore them.
