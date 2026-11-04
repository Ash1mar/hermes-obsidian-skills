# Automatic QA handling and bounded recovery

New short requests pin `scope.recovery_policy`, including the QA rule, before Bootstrap.
Changing shared configuration does not alter an existing pinned workflow. Legacy
workflows without the policy retain their documented configuration fallback. The rule is:
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

For new pinned workflows the dispatcher holds the first plan before any native
attempt. A drained program controller creates its deterministic draft preflight
and admits `initial-release-preflight/v1`; there is no failed native task.
Legacy workflows keep the supported failure-recovery path. Both seal the exact Bootstrap, draft plan,
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
Partial Canary coverage is reported as partial, never as complete sampled acceptance. Excluded work is unavailable to Reduce and cannot be published.

Conversion defects, changed or absent required evidence, permissions, transport
failures and unknown native completion do not gain an unchanged-material retry.
Unauthenticated failures stay held. Recovery cannot cross a durable pause.
One additional check is an upper bound, never a prerequisite for isolation.

New short requests pin `unknown_completion=isolate_after_drain`. For an exact
stopped unknown Pass call, the controller persists its intent, cancels/drains the
workflow and calls `skip-pass-slices` with `deferral=automatic_unknown_quarantine`.
It authenticates the hash-bound call failure, native request, ended run, current
binding and exact unfinished reserved task set. A returned response must be
verified first. Completed tasks survive; unaccounted tasks, live owners, changed
evidence and foreign/unselected Canary work remain held. No model retry occurs.
The original unknown outcome and every reservation stay intact; the coverage-gap
report excludes the tasks and their actual dependents from Reduce/publication.
Restart replays the same saved exclusion/resume identities, without new quota.

Existing pinned `hold` policies and legacy fallbacks do not change silently.
After explicit workflow-wide authorization, stop/drain the workflow and invoke
`python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault <path>
adopt-unknown-isolation --request <request.json>` (preview with `--dry-run`).
The request names workflow/actor/expected_revision, stable operation_id, reason,
`unknown_completion=isolate_after_drain`, previous_policy_sha256 (the effective
policy fingerprint), and ordinary mutation input_digest. Adoption appends an
audited policy overlay, preserves startup scope, and does not resume or release a
pause. A pending recovery intent must finish before adoption. One adoption covers
later eligible Pass incidents; no per-incident approval is required. The explicit
`unknown_native_quarantine` path remains available for an authorized exact scope.
This contract is for Pass candidate/citation requests; unknown release/Provider
side effects still require their own authenticated recovery, never blind replay.

Budget-lock contention waits up to five seconds for the atomic ledger operation.
An ended worker blocked on this exact lock can receive one mechanical recovery,
including within the selected Canary. Admission pairs every observed request with
its returned native transcript and durable audit. It settles only authenticated
reserved budget entries, preserving their ordinals; unknown calls remain held.
The next native worker reuses saved responses through ordinary evidence checks.
This grants no extra semantic allowance and does not recreate accepted Passes.
The watcher selects the installed Hermes interpreter before taking its ownership
lock when the short-entry system Python cannot import the native runtime.

`scripts/recover_ingest_workflow.py` is the same controller used automatically by
the watcher. A pending controller episode can be continued without selecting
individual incidents; a user cancellation or durable pause cannot be released by it. Invoke with `python3 "<skill-dir>/scripts/recover_ingest_workflow.py"
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

The recovery supervisor reserves at most three mechanical attempts in one durable
episode. It restarts a crashed child and retries only named transient coordination
failures. Unknown domain calls and uncertain operator mutations stay held. An
operator cancellation invalidates the saved continuation epoch, including when the
workflow was already stopped by the controller. Restarting with the same episode
does not renew either the controller budget or the one-extra-semantic-check limit.
No reboot service or scheduled job is installed. If the supervisor itself is lost,
the authorized recovery entry discovers its unfinished episode on reinvocation.
