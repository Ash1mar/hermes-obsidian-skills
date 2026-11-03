# Automatic QA handling and bounded recovery

`qa_policy` in `config/orchestration.json` fixes the all-stage program rule:
QA/`needs-qa` labels are advisory. Usable evidence continues with its labels;
concrete blocked work is isolated with its actual dependents. No person or Codex
adjudicates QA. One additional Hermes check is the maximum, not a mandatory call.
Original results, consumed attempts and unresolved coverage remain auditable.

The same policy covers Bootstrap/source preparation, Exact Plan, Canary/Pass,
both Reduce phases, both checkpoints, Build, Release Plan/Apply, Provider Sync
and Final Acceptance. Mechanical checkpoints still validate identities, hashes,
coverage and release/index state; a QA label alone does not fail them. Missing
required evidence, permissions and unknown execution remain separate contracts.
Only supported, verified delivery scope is released or indexed; necessary QA
gaps produce partial acceptance, never invented approval or complete coverage.

Source Reduce preserves valid packets and records explicit omissions. Global
Reduce preserves native bodies, omits blocked pages and follows actual page
links when excluding dependents. Build uses existing native partial-commit and
delivery-closure controls: accepted pages survive, deferred pages are excluded,
and every original review remains. These transitions are program controlled.

New short requests pin source-Reduce worker concurrency 10 independently from
other program workers 2 and page-call concurrency 10. Pass remains 10 and source
preparation 2. Host capacity/memory guards continue to limit actual dispatch.

Continuous `auto_full` workflows default to `pass_failure_policy=once_then_isolate`
in `config/orchestration.json`. `hold` disables automatic recovery. Staged and
canary-only workflows retain their explicit operator boundaries.

The native reconciler hands a drained blocked Pass stage to the program recovery
controller. It authenticates ended native sessions, request/response hashes,
current reading inputs and canonical task ownership. Each task/material/phase has
a durable call budget: one initial request plus at most one correction. Changing
workers, regrouping tasks or restarting the program does not reset that budget.
Existing native calls are adopted only with authenticated evidence.

After the correction fails, the controller uses the supported exclusion operation
for the unfinished tasks, preserves completed tasks and every original response,
and writes an auditable coverage-gap report before resuming independent work.
Partial Canary coverage is reported as partial, never as successful eight-slice
acceptance. Excluded work is unavailable to Reduce and cannot be published.

Conversion defects, changed or absent required evidence, permissions, transport
failures and unknown native completion do not gain an unchanged-material retry.
Unauthenticated failures stay held. Recovery cannot cross a durable pause.

`scripts/recover_ingest_workflow.py` is the same controller used automatically by
the watcher; it can also continue a maintenance-stopped workflow without selecting
individual incidents. Invoke with `python3 "<skill-dir>/scripts/recover_ingest_workflow.py"
--vault <explicit-path> --workflow-id <id>`. It authors no domain output.
