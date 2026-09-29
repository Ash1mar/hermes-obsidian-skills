# v7 worker dispatch acceptance

The dispatch contract covers real Gateway workers and isolated terminals. Passing
unit tests or invoking the planner directly is insufficient deployment evidence.
Hermes core and persistent host configuration are outside this change.
In `canary_only`, the batch still plans the full ready scope, while the native
projection creates only its eight selected Pass cards. Unselected tasks and
Reduce/release nodes stay in the Vault plan until full execution is authorized;
this avoids hundreds of unnecessary native CLI calls during a bounded trial.

## Gates

1. Rejected missing fields, aliases, wrong hashes/tasks, cancelled/superseded
   bindings and execution blockers must not commit domain state. Same valid
   binding must not rewrite request artifacts or increment ledger revision.
2. An isolated Hermes root and Kanban root must be verified before Gateway startup.
   Gateway dispatches a native card to the configured real model. Its terminal
   executes the supplied command; the helper commits a batch and the reconciler
   acknowledges it. Do not manually patch the positive case's card/request.
3. Inject a rejected worker request. Observe helper failure evidence and native
   archival, then at least two full dispatch cycles without another attempt or
   successor. Archive descendants first: native archived parents satisfy edges.
4. Clone current ready inputs into a disposable Vault. Kill the bound planner
   after measurement persistence, resume it, verify reuse and commit one batch.
   Every core UnitRef appears exactly once and every committed task's measured
   serialized window fits the unchanged reader limit.

Native worker termination is reconciled against durable slice failures from that
run. A recorded model/semantic failure retains its existing bounded retry or
approval state; it is not reclassified as a dispatch contract failure. A later
native run ending without its own Vault outcome is quarantined. Regression
coverage exercises both cases and the elapsed retry deadline.

Retain request digests, native run events, helper execution/progress records,
typed failure reports, retry counts, timings and output coverage. Model outage or
an unavailable native interface leaves its gate unpassed. Observe a small real
case for at most ten minutes before recording a service/contract blocker.

## 2026-09-29 evidence

Machine-readable evidence is under the workspace's `tmp/hermes-deploy/`:

- `v7-native-acceptance.json`: isolated Gateway → real model → terminal →
  preparation helper committed the small exact batch in 35.08 seconds. Native
  request-artifact corruption was archived with stable attempt count across two
  further dispatch cycles.
- `v7-native-worker-failure.json`: real worker invoked the helper; wrong hash
  returned `STALE_INPUT` and persisted `execution_blocked`. Archival prevented
  additional attempts over two further dispatch cycles (27.82 seconds to failure).
- `v7-scale-acceptance.json`: seven actual ready sources, 3,227 core units,
  880 tasks, 1,353 persisted measurements. Kill occurred after 13 measurements;
  resumed helper computed 1,340 new measurements and reported 893 cache reuses
  (including repeated lookups within the session). Every committed task fit
  12,000 serialized codepoints. Original HBTest2 ledger remained unchanged.

The scale helper completed within 280 seconds including kill/restart observation.
The audit script's initial wrong output filename was corrected before checking
the persisted `plan-request.json`; it did not change the committed plan.

Deployment follows main validation/commit/push, a clean same-worktree merge to
intranet preserving its six deployment configurations, intranet validation/push,
then main Skill overwrite without backups. Migrate the original pre-batch
workflow through cancel → repair without source resets → resume. Run the original
generic prompt to obtain a separate eight-slice Canary result; exact-plan
acceptance alone does not assert Canary completion.
