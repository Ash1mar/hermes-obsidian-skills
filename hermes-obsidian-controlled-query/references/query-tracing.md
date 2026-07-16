# Query Trace Contract

Use query traces to audit retrieval navigation without turning runtime history into source evidence.

## Authority and write boundary

- Write only under `_system/reports/query-traces/`.
- Treat Markdown traces and JSON sidecars as non-authoritative, disposable runtime records.
- Never cite a query trace as evidence and never retrieve it as a governed answer source.
- Do not copy long source passages, credentials, hidden prompts, or unrestricted tool output into a trace.
- Keep the rest of the Vault read-only unless the user separately authorizes controlled writeback.
- Logging failure must not block or weaken the answer. Mention the failure in the answer only when auditability was explicitly requested.

The default is one trace per controlled query. Skip it only when the user explicitly requests no logging or the Vault cannot be written.

## Lifecycle

Start immediately after resolving the Vault and Hermes session ID:

```bash
python3 scripts/manage_query_trace.py start <vault-root> "<question>" \
  --session-id <hermes-session-id> --query-type <type>
```

Retain the returned `trace_id`. Append an event after each attempted retrieval layer, including zero-hit and failed stages:

```bash
python3 scripts/manage_query_trace.py event <vault-root> <trace-id> \
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
python3 scripts/manage_query_trace.py finish <vault-root> <trace-id> \
  --status completed --evidence-level source-backed \
  --conclusion "Short statement of what the checked evidence supports." \
  --unresolved "Table image still requires visual QA."
```

If execution stops after a trace starts, finish it as `failed` or `incomplete` when possible. Because every event is persisted immediately, a crash still leaves a readable partial trace.

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
