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

First-Vault release planning also runs under program control. For a short-request
Bootstrap workflow whose first draft has no deliverable pages, the drained
controller previews and applies the existing source-governance transaction.
Only hash-valid registered sources and closed organization declarations can
establish identity. Unresolved sources are logged and their dependent pages stay
excluded; independently resolved sources can proceed. Zero resolved sources
remain held, without guessing an organization or asking for per-item human QA.

The supported release recovery seals the exact Bootstrap, rejected draft plan,
source transaction and receipt. The native planning worker then qualifies only
current pages in the exact accepted Build delivery closure for internal retrieval.
Usable pages still require valid acceptance, current evidence and eligible source
governance. QA pages and actual dependents remain excluded. This initial path
does not fabricate a previous applied release, clear QA, change Provider scope,
or add a pause. It grants zero additional model calls; the existing maximum of
one extra Hermes check is unchanged. Recovery cannot grant another initial
qualification after a newly failed plan, and interruption reuses the same intent.

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

After successful or partial Final Acceptance the watcher remains responsible
for native completion acknowledgement. It verifies the final release and
acceptance artifacts, runs supported reconciliation, then waits for native
consumers, runnable cards and leases to drain before exiting. Restarting that
watcher repeats the same idempotent mechanical operation. Terminal workflows
cannot dispatch new workers through graph eligibility or the native dispatcher.

This bounded settlement does not execute acceptance, synchronize the Provider,
reset semantic budgets or grant a retry. QA, coverage gaps and valid results stay
unchanged. Cancelled and failed workflows retain their stop behavior. Unknown or
invalid final evidence remains held; transient lock/native-CLI contention is
polled for at most 60 seconds and recorded in the host runtime's
`terminal-completion.json`. An unresolved settlement is reported as an error,
never as successful native completion. No scheduled job or human QA is involved.
