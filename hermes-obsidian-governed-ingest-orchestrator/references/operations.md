# Operations

Candidates use existing compact rule/topic, applicability, condition/exception,
support and inspection QA fields. Citation still checks original facts and
omissions. Downstream Reduce, page writing and page review receive original
hash-valid reading packages and required context from program-controlled
projection, with exact support spans. Old valid Passes are not rewritten.
Missing required inspection, stale authority or oversized evidence holds the
affected request; no evidence truncation or candidate-only fallback is allowed.
Map observations do not validate Reduce/review costs or release the Pass pause.

All examples use `python3 "<skill-dir>/scripts/<script>.py" --vault "<vault>" <command>`. Request-taking commands read JSON from `--request "<file>"`; never embed secrets in CLI arguments.

| Intent | Command | Result |
| --- | --- | --- |
| Start once | `dispatch_ingest_workflow.py start` | Vault record + pinned templates; attempts Kanban projection |
| Inspect | `manage_ingest_workflow.py status --workflow-id <id> --compact --runtime` | Authoritative stage, revision, checkpoints plus read-only native attempts |
| Preview canary | `manage_ingest_workflow.py canary-preview --workflow-id <id> --limit 8` | Read-only first eight ready slice IDs and selection digest |
| Arm canary | `dispatch_ingest_workflow.py arm-canary` | Persist the exact previewed allowlist and project only those Pass cards |
| Prepare one source | `dispatch_ingest_workflow.py worker-prepare-source` | Bound registration, supervised PDF conversion, QA and SourceUnit publication; preserves pending on runtime/review errors |
| Source worker result | `dispatch_ingest_workflow.py worker-complete` or `worker-fail` | Commit one validated ready UnitSet or one typed failed-source coverage gap; continue other source cards |
| Exact plan worker result | `dispatch_ingest_workflow.py worker-complete` | Adopt the exact-budget batch after every source has a result; only ready UnitSets may enter the batch |
| Stop canary | `dispatch_ingest_workflow.py disarm-canary` | Persist disabled policy; subsequent worker checks fail closed |
| Resume | `dispatch_ingest_workflow.py resume` | Clears permitted stop, then resynchronizes projection |
| Continue one reviewed pause | `dispatch_ingest_workflow.py continue-workflow` | Revalidates evidence, records explicit authorization and releases only this boundary |
| Approve in manual mode | `manage_ingest_workflow.py approve` | Records named human checkpoint with approval digest |
| Cancel | `dispatch_ingest_workflow.py cancel` | Persists cancellation and parks projected cards in scheduled until explicit resume |
| Recover board | `manage_ingest_workflow.py rebuild-kanban`, then `dispatch_ingest_workflow.py sync` | Rebuilds projection from Vault |
| Recover reconciler | `watch_ingest_workflow.py --workflow-id <id>` from a trusted non-worker terminal | Continues Kanban acknowledgement and successor dispatch from Vault outcomes |

Before any mutation inspect current status and compute a fresh digest with `manage_ingest_workflow.py digest`. Gateway/dispatcher failure after start is a recoverable result: `workflow_created: true`, `state: dispatcher_unavailable`, `background_dispatch: false`. Do not fall back to a long conversational run. In `auto_full`, each checkpoint worker validates the exact Vault state, writes a result report, and calls `worker-complete`; the adapter writes a hashed decision and the workflow service independently verifies it. Passing decisions automatically advance the gate. Failed validation leaves the card blocked and exposes report and blocking codes. Manual mode still accepts explicit human approval.

For a dedicated fixed-program ingest Gateway, start the existing service with
`HERMES_KANBAN_DISPATCH_IN_GATEWAY=0 hermes gateway run`. The trusted ingest
dispatcher owns program-card claims; the Gateway must not run the generic
triage decomposer or open model dispatcher for this scoped observation. This
process-local setting does not alter another Gateway's host configuration.
Program-card pauses and capacity holds use native `scheduled`, which is not
dispatchable until the trusted adapter explicitly unblocks the same identity.
Do not use repeated human `block` operations for these holds: native recurrence
routing can move a card into `triage`. Unexpected triage requires trusted
isolation recovery, never automatic decomposition or an open-agent fallback.
The native cancellation control receipt now waits for actual workers and the
reconciler to exit and checks the dedicated board for ready/running/triage cards,
including cards absent from the Vault map. It rechecks this proof on receipt
replay. A timeout or routing conflict leaves cancellation verification pending.

Current `worker_dispatch_enabled: true` permits scoped source workers and exact planning after coverage checks. In `auto_full`, the first eight ready slices form a bounded canary; once all eight have durable valid Pass records, remaining slices and the downstream workers become eligible in order. Manual mode retains explicit canary arm and approval operations. Inspect compact status for `source_coverage` before planning or reporting completion. Failed sources are listed as gaps and require a partial final workflow outcome.

## Staged pauses and explicit continuation

For a staged full run set `scope.execution_mode: auto_full`, keep the desired Provider
choice (`sync` or `skip`), and set:

```json
"pause_after": ["exact_plan", "canary", "pass", "checkpoint_1", "build_finalize", "checkpoint_2", "release_sync"]
```

| Boundary | Completed evidence | Next work held |
| --- | --- | --- |
| `exact_plan` | Adopted batch and initialized slices | Canary arming and every Pass worker |
| `canary` | Exactly eight pinned slices with durable Pass results | Promotion and remaining Pass |
| `pass` | All slices and planned tasks have candidate/citation Pass coverage | Every Reduce worker |
| `checkpoint_1` | Passing validation and verified decision | Build Finalize |
| `build_finalize` | Completed build runs | Vault Finalize release planning |
| `checkpoint_2` | Passing release-plan validation and verified decision | Release apply |
| `release_sync` | Applied release and requested Provider sync | Final acceptance |

With `provider: skip`, `release_sync` records an explicit skipped Provider, not a
successful index build. Checkpoints still validate automatically; a configured
pause requires a separate operator continuation before their successors dispatch.

Inspect `status --compact --runtime` and review `pause.report_ref`. On explicit user
authorization, construct the following request from the latest observed ledger,
compute its `input_digest` with `digest`, and call:
`python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" continue-workflow --request "<request.json>"`.

```json
{
  "workflow_id": "<observed workflow>",
  "actor": "<observed actor>",
  "expected_revision": 123,
  "continue_id": "<stable ID for this authorization>",
  "boundary": "<observed pause boundary>",
  "evidence_digest": "<observed pause evidence_digest>",
  "input_digest": "<complete mutation digest>"
}
```

Continuation releases exactly one boundary. Reuse the exact request on retry.
`resume`, `sync`, board rebuild and Gateway restart never grant continuation.
The watcher exits after acknowledging completed cards and reporting a pause; the
dispatch continuation command starts the next reconciler when work becomes eligible.
Worker begin/check and domain writes also enforce an unlatched reached boundary,
so delayed reconciliation does not open a write window. A completed boundary owner
may submit its durable completion while successors remain held. Changed evidence
or an execution blocker must be inspected through supported operations; never edit
the pause record, replay an old authorization for a new boundary or silently create
a replacement workflow. Do not change an existing workflow's immutable pause policy.

Staged workflows project only the current DAG prefix. An exact-plan pause projects
no Pass cards; the canary segment projects only its eight selected slices. Full slice
and task coverage remains in the Vault. Later segments add their cards on continuation;
unopened phases are not mistaken for missing domain results or completed native work.

## Workflow lock recovery

Pass workers preflight each semantic draft with the bound `batch-pass --validate-only`
command. It checks the same schema, task revision, reading window and live references
as persistence, without creating Pass/task/batch records or failure state. A worker
may correct an `INVALID_SCHEMA` in its own draft from observed package references
and retry preflight at most twice. This never permits changing a binding, source or
ledger, recovering a stopped lease or crossing a pause. Persist only the draft that
passed preflight; unresolved errors retain their evidence and stop the slice.

New workflow, dispatch and watcher locks use host kernel locks. Their stable kernel files live
outside the Vault (`~/.cache/hermes-skill-runtime/locks` on Linux; the user temporary directory
on Windows). Vault owner markers describe the holder but do not determine occupancy.
Process exit, SIGTERM, SIGKILL or OOM releases the kernel lock; the next local invocation
can replace a leftover marker. Never delete kernel lock files while processes may be running.
`LOCK_BUSY` means actual contention; `REVISION_CONFLICT` means a ledger revision conflict.
Worker domain writes wait at most ten seconds for a workflow lock, checking cancellation,
card identity and pinned contracts while waiting and again under the acquired lock.
Only lock acquisition is retried; a domain mutation is never replayed by this wait.
`LOCK_TIMEOUT` means that bounded wait was exhausted and remains an execution blocker,
not a failed source. Exact-plan completion rebuilds its checks and revision on each
rejected lock/revision attempt within the same ten-second bound.
`LOCK_RECOVERY_REQUIRED` means a legacy PID marker or a marker from another host requires review.

For old plain-PID workflow locks, use the trusted operator command:
`python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" recover-locks --request "<request.json>"`.
The request includes workflow_id, actor, expected_revision, input_digest, recovery_id, reason,
and locks: each entry has a full Vault-relative path, expected_owner_pid and sha256 (prefixed `sha256:`).
Only this workflow's `.locks`, `.dispatch-locks` and `.watch-locks` markers are permitted.
Run recovery on the verified original runtime host. The operation acquires all relevant kernel
locks, rechecks the current ledger and every pinned legacy marker, verifies the PIDs are absent,
and writes a recovery audit report before removing the markers. It refuses live or unverifiable
owners, changed content and isolated workers. It does not change workflow revision or source outcomes.
Recovery is idempotent for the same request and ID. Recover both dispatch and watcher markers,
then verify Gateway is running and use normal resume/sync; opening dashboard TUI alone does not
start the Gateway. Foreign-host JSON markers cannot be removed by this plain-PID recovery command.

## Auditable pre-batch repair

### Explicit Pass gaps and grouped program staging

With user authorization, `skip-pass-slices` excludes failed content without declaring it
verified. Save a current actor/revision/digest request with a stable `operation_id` and
`entries`: each has `slice_id`, `reason`, and `evidence` (`ref`, `sha256`) covering its
task ownership through bound current or superseded failure reports. Preview then apply
If only part of an unfinished slice has failed evidence, explicit `scope: whole_failed_slice`
may conservatively exclude the allocation as a whole. The report distinguishes known
failure task IDs from withheld unverified siblings; it does not claim every task failed.
the same request using `python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault
"<vault>" skip-pass-slices --dry-run --request "<request.json>"`, then omit `--dry-run`.
The workflow and batch must be cancelled and quiescent; completed slices, source IDs,
targets, readings and every Pass remain. A recoverable journal serializes task status
updates and the gap manifest; interrupted updates block execution until that exact
request finishes. Semantic/material refusals, unresolved original model requests,
duplicate content and missing inspections can be excluded as gaps; their ownership
is not automatically labelled a model error. Program budget/lease/permission failures
must be repaired separately. Excluded ranges are never passed to Reduce as evidence.

Only the fixed program worker may use `program_phase_groups/v1` staging: its aggregate
packet is bounded by task count and the existing per-task input cap, and never delivered
whole to the model. Candidate/citation groups still check full actual messages/schema,
images, input/output and reserve limits before each SDK call. No truncation, expanded
reading authority, new task IDs or model budget increase is permitted. An indivisible
oversized task stays unresolved. After this evidenced admission fix, a selected budget
failure can use `recover-pass-execution` with `recovery_scope: phase-packet-grouping/v1`;
the program verifies the pinned fixed phase policy and allows one attempt for that
distinct corrected cause. It cannot continually regrant the same budget failure.

A complete phase request that fits its existing input, output and image bounds but
fails only the correction reserve may be admitted as `single_attempt_no_correction`.
Its plan and call audit record one attempt and zero correction reserve; an invalid
response preserves partial results and stays held without a model correction call.
No materials or task identities change. `correction-reserve-admission/v1` recovery
accepts only the matching structured reserve-only failure, with the current pinned
phase policy and the ordinary identity, hash and quiescence guards; one attempt per
corrected cause remains enforced. Actual oversized input/output/images stay blocked.

When all Pass slices are completed or explicitly excluded, the normal Pass pause
includes hashed gap reports and records a partial outcome. No automatic continuation
is implied. After separate continuation, reducer task lists and candidate evidence
exclude skipped tasks; downstream validation retains skipped coverage and final
workflow acceptance is partial. Partial products are not full-source verification.

An explicit `deferral: bounded_input` entry in `skip-pass-slices` may exclude an
unfinished citation whose complete current request truly exceeds the unchanged
input cap. The trusted adapter verifies the current failed native binding, source
bytes, pinned policy, persisted candidate and full citation plan under the same
locks, and embeds that plan and packet hash in the gap report. A fitting request,
candidate not yet produced, stale evidence, worker-origin request, permission
failure or unknown program failure cannot use this deferral. It preserves every
Pass and original reading, marks no verification success and excludes the selected
allocation from downstream evidence. This is explicit budget deferral, not content
misclassification or automatic retry. The ordinary dry-run/apply operation remains.

Before a blocked coordinator exits, it runs the existing fast settlement until its
exact ended Pass leases are cleared, with a 30-second bound. Native private grant,
ended run and canonical binding checks remain; current native process inventory
protects live workers even when the public JSON omits the PID. A stale recorded PID
alone no longer keeps a dead worker's lease indefinitely. Unverified or unsettled
shutdown reports an explicit error and keeps the affected work guarded.

### Selected mechanical Pass recovery

Only a trusted operator with user authorization may recover selected cancelled unfinished
Pass slices. Keep the workflow and batch cancelled, with no pending amendment, live lease,
native run, worker/reconciler or dispatchable board card. Canary must already be released;
no held boundary or unaccepted semantic revision may be crossed. Correct the evidenced
mechanical cause before selecting a retry.

Save one request outside the Vault with `workflow_id`, current `actor`, `expected_revision`,
stable `recovery_id`, `reason`, and `entries` containing each exact `node`, current native
`task_id` and SHA-256 of its current failure report as `failure_sha256`. Compute the complete
`input_digest` using the existing mutation digest contract. Preview and apply the same file:

```bash
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" recover-pass-execution --dry-run --request "<request.json>"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" recover-pass-execution --request "<request.json>"
```

The program accepts only `WORKFLOW_STOPPED`, or structured `PASS_EXECUTION_BLOCKED`
failures whose every constituent is `WORKFLOW_STOPPED`/`LOCK_TIMEOUT`. Unknown, mixed,
budget, coverage, permission, material and semantic failures remain guarded. It rechecks
source bytes, planned measurements, current bindings, ended native attempts and runtime
quiescence. The recovery audit preserves the original failure payload and hash. One atomic
workflow append changes only selected native execution fingerprints; old bindings, slice
identities, domain inputs, partial/complete Passes, budgets and pause history remain intact.
Interrupted report-only writes can finish using the same request; replay acknowledges the
same record and never grants an extra attempt. At most one recovery is accepted per slice.
Recovery leaves everything cancelled. A separately authorized normal resume projects fresh
native cards for selected slices; old/foreign grants remain rejected. Continue to the next
durable pause or stop when no runnable work remains, without marking deferred slices done.

`python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" repair-preparation --request "<request.json>"` requires a cancelled workflow with no batch. Include workflow_id, actor, expected_revision, input_digest, a stable repair_id, reason, existing Vault evidence_refs, complete templates from the installed orchestration.worker_pack(), and optional execution_mode. Each reset_sources entry must pin path and outcome_digest (`sha256:` plus fingerprint of the current failed outcome). Only those failed outcomes are reset; valid registrations and other source outcomes remain. The before.json snapshot, its hash and repair history retain the old record. Old template snapshots stay immutable and old cards lose their binding. The command is idempotent for the same repair ID and digest, and does not resume work. A subsequent normal resume projects new cards from the repaired contracts.

For a bounded trial choose canary_only. All requested sources are prepared (real failed files remain explicit gaps), exact planning runs, and exactly eight ready Pass slices run. The dispatcher writes reports/canary.json after their durable completion, then stops eligibility; no automatic promotion, Reduce or release follows. Fewer than eight ready slices reports canary_unavailable. Use auto_full only for an explicitly requested full run.

worker-register-source is the standard source identity allocator. Runtime/contract errors create a Vault failure report without writing a failed-source outcome; the trusted reconciler blocks the card. Domain governance commits and SourceUnit publication use an explicit `--worker-binding` request with workflow_id, task_id and node, verified under the same workflow lock as cancellation. Successful cancellation therefore prevents subsequent authoritative source commits even when Hermes strips task environment variables; in-flight conversion scratch output is not a committed artifact. Worker commands never call the Kanban CLI. These cooperative worker guards are not an OS security sandbox.

## Runtime status and deterministic source preparation

`status --compact --runtime` reads native task/run JSON without syncing cards or writing the ledger.
Each card reports `kanban_status`, `state`, attempt identity/timestamps and an evidence-based
`error_category`. A requeued closed attempt is `retry_wait`; a new active run is `running`, with
`last_failed_attempt` retained separately as history. Explicit APIConnectionError/ConnectError
becomes `model_connection_failed`; explicit rate-limit/429 and quota messages remain separate.
Hermes outcome `rate_limited`, its synthesized `pid ... exited rate-limited (quota wall)`
sentence, or exit 75 alone is `provider_retry_reason_unknown`. For running/retry/blocked cards,
status also reads at most 64 KiB of task log and reports explicit API error categories under
`log_observations`; these log hints have no reliable attempt binding and must not be presented
as proof that the current attempt failed. Missing logs remain explicitly unavailable.
The snapshot does not expose a guaranteed retry deadline, so `retry_not_before` stays null.
Native read failure is `unknown`, never successful running. These are per-card observations,
not an atomic board snapshot; errors not yet persisted by Hermes remain unknown. Do not infer
source failure or model recovery solely from these labels. Source coverage is still the ledger.

New workflows pin the installed contracts. Preparation workers execute the card's complete `worker_command`,
which reads its immutable dispatcher binding. Missing/changed artifacts stop for trusted repair;
sync never overwrites a changed request. The helper owns begin/check/complete and makes no chat-model calls. It never
calls the Kanban CLI. Local conversion retains the shared host gate and cancellation monitoring.
Only this workflow/source's deterministic attempt directories are automatically resumed; use
`bundle` for explicitly requested existing Bundle reuse. Already current units are validated
against the assigned raw hash and identity before reuse. A validation failure allows one supported
pipeline retry with both attempts preserved. The converter writes `conversion-result.json`
(`hermes-conversion-result/v1`) bound to raw SHA-256 and backend. A nonzero local engine
exit with a terminal typed PDFium document-load exception for format, password or security
can report `document_failed`; arbitrary log words, file-access errors, model errors, signals,
supervisor failures and missing output are execution failures. Only two proven document
failures close the assigned source as `CONVERSION_FAILED`, retaining both diagnostics and logs.
Runtime/config/unknown errors and unresolved Bundle validation leave the source pending.
Attempt directories and logs are scoped to the binding generation; repair preserves previous evidence.
Failed helper execution persists a typed
execution blocker; the trusted reconciler closes the native card and dependent subtree. Do not
restart the same failed binding or manufacture a missing request field.
Sync retains the current Vault-bound failed card even if native Kanban has archived it; native create
does not deduplicate archived keys. An explicit pre-batch cancel/repair/resume creates a new
generation and invalidates old workers. Use the operator helper
`python3 "<skill-dir>/scripts/repair_preparation.py" --vault "<vault>" --workflow-id "<id>" --repair-id "<repair-id>" --evidence-ref "<failure-report>"`
to preview the request, then repeat with `--apply`. It leaves the workflow cancelled.
For pending sources use no reset selector: ready and failed outcomes are preserved. Resetting
a failed source additionally requires its exact `--reset-source-sha256` and an existing typed
failed outcome; the request pins the outcome digest and the service verifies unchanged raw bytes.
Repair is an explicit operator operation, never an automatic response to source damage or a resume.
Completing one source does not enable exact planning/Canary until the remaining sources finish.
Changing installed templates never updates an existing workflow's pinned pack automatically.

## Exact planning and pre-batch recovery

Execute exact-plan's `worker_command` as a single supervised background terminal job (`background=true,
notify=true`), polling in short calls rather than waiting through the outer tool deadline.
The helper validates full ready coverage, accumulates whole Units in source order,
and closes a window before adding the next Unit would exceed canonical or model
budget. New plans use the same runtime projection, required registered dependencies,
QA, complete images, strict wire schema and phase-budget estimates. Candidate wire
size is measured; future candidate content in citation is conservatively reserved
as an explicit heuristic and checked against actual content during execution.
New batches pin the existing phase budget policy; existing batches retain their
identities and policies. The helper
and persists per-window measurements plus `progress.json` and `plan-request.json` under
`_system/reports/exact-planning/<input-fingerprint>/`. Restart the same bound command to reuse
verified checkpoints while the binding remains valid. A reported contract failure requires the supported repair
below before rerunning; a repair epoch changes card identity but preserves compatible measurements. Registry/ACL, UnitSet bytes, reader configuration and input fingerprints
must still match. An indivisible oversize unit stays explicitly blocked. Commit reuses the
same verified measurements instead of recalculating the entire plan.
There is no fixed eight-Unit initial window, task reordering or old-task migration.
The library's reader-only mode remains available for canonical reading measurements;
the native exact worker always supplies the actual Pass model-budget measurement.
For read-only parameter measurement use
`python3 "<skill-dir>/scripts/measure_exact_plan.py" --vault "<vault>" --workflow-id "<id>" --reader-limit 12000`
and optionally repeat `--reader-limit` or select a registered `--resource-id`.
This runs the same planner without checkpoints, tasks, batches or model calls;
indivisible oversize material and the bounded measurement timeout are reported.
It does not revise the selected existing workflow or authorize a new workflow.
The generic reader selects at most eight nearest eligible context candidates before
serialized-budget trimming, so omitted-context metadata cannot grow with an entire
large ancestor section. Every ready source's core units are still planned exactly once;
the reading limit is unchanged and never adjusted for a filename. The context-selection
fingerprint is versioned, invalidating measurements made with the previous rule.

For an already cancelled workflow without a batch, an operator can preview
`python3 "<skill-dir>/scripts/repair_preparation.py" --vault "<vault>" --workflow-id "<id>"
--repair-id "<stable-id>" --evidence-ref "<Vault-relative-failure-report>"`.
The evidence ref is required when no source is reset. Add `--reset-source-sha256
"<failed-source-raw-sha256>"` only when the selected failed SourceUnit outcome truly needs
replacement. Add `--apply` to archive superseded bound cards, install the complete pinned
pack and reset only explicitly selected failed SourceUnit outcomes. This never resumes. Use the normal governed
`resume` command afterward with the same workflow ID. Do not invoke repair from a worker.
The auditable repair snapshot preserves previous results; it is a workflow record, not a
backup of installed Skills. The source helper may reuse that selected source's QA Bundle
from the verified repair snapshot. New Bundles go under `_system/reports/source-bundles`;
legacy Bundles may be read without rewriting `10_Raw`.

## Remaining Pass input and packaging changes

Use the operator-only [compact execution contract](compact-execution.md) when explicitly
authorized to reduce model input or repack unfinished tasks in a cancelled Pass batch.
It preserves exact-plan, canonical identities, accepted results and all continuation/pause
records. The prepare helper creates the request; dispatcher `amend-execution --dry-run`
previews it; `amend-execution` applies the same request and remains cancelled.
Resume and continuation are independent authorized actions. Workers never migrate their own
input contracts or read mapping manifests. Their compact draft still passes the ordinary
bound preflight, canonical validation and durable write pipeline.

## Bounded Reduce cancellation and recovery

Before a bounded run, inspect the existing workflow's pinned Reduce templates and
the executor selected for its native cards. The fixed concurrency bound and semantic
response recovery below apply to fixed program workers, not legacy `model` cards.
Installing this Skill does not migrate those immutable pins. A legacy Reduce workflow
without a supported contract amendment must remain stopped; do not enable general
Gateway dispatch, replace its bindings or repeat resume to make this check pass.
No in-flight stop or semantic recovery acceptance can be claimed without a real
compatible native Reduce attempt.

### Before the first legacy Reduce attempt

With explicit migration authorization, prepare a request naming the observed workflow,
stable migration ID and reason. It pins the current source contracts and installed
target hashes; it does not mutate the workflow:

```bash
python3 "<skill-dir>/scripts/prepare_reduce_migration.py" --vault "<vault>" --workflow-id "<observed-id>" --migration-id "<stable-id>" --reason "<authorized reason>" --output "<request.json>"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" migrate-reduce-execution --dry-run --request "<request.json>"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" migrate-reduce-execution --request "<request.json>"
```

The trusted operator reviews and applies the same request. It requires cancelled
workflow/batch, a released Reduce stage, no live consumers/leases/journal or dispatchable
native cards, complete non-skipped Pass evidence, unchanged source/measurement identities,
and no actual Reduce attempt, bound failure, reduction, build or checkpoint result.
Zero-duration cancellation parking rows without a worker PID are administrative records;
other native attempts are rejected. This is not a retry or a migration of prior results.

Only `resource-reduce`, `global-reduce` and `checkpoint-1-validate` active pins change.
The installed fixed templates must match the requested target hashes. Original template
files and bindings remain audit evidence; a hashed migration report records both pin sets
and previous native identities. One atomic workflow write activates the new pins and
history, without changing batch/domain artifacts, Pass identities, budgets, gaps, policies,
pause history or current cancelled state. Interrupted immutable staging can replay the
same request; a different migration ID cannot repeat the migration. A later authorized
resume projects fresh Reduce cards, archives obsolete cards through normal reconciliation
and rejects old bindings. Changed migration evidence or template bytes block execution.
Build Finalize, later stages, pause release, publishing and Provider sync are not migrated
or authorized by this operation.

### Bounded fixed execution

Source reducers first plan sequential candidate packets with complete original packages,
instructions, schema and image limits. Each packet returns proposals only; normal
source submission verifies the union exactly once, and global coordination handles
relationships across those packets. Exact duplicates may share a handle within a
reading; distinct readings stay independently packetable. No full-source fallback,
truncation, topic classifier or extra native scheduling layer is introduced.

For a current source `READING_WINDOW_OVERSIZE` failure, the trusted preparation script
`prepare_reduce_repairs.py` can create a `recover-reduce-execution` request using
`recovery_scope: source-packets/v1` and `target_contract_sha256`. Dry-run verifies the
exact ended failure/binding and proves that every complete candidate packet now fits
the installed request/image contract before granting one fresh native identity.
It preserves domain inputs and old failures, never resumes, and does not authorize
an unchanged indivisible oversize, inspection gap, semantic failure or global retry.
Cancellation and packet correction have separate one-attempt scopes; a repeated failure
under the same cause requires diagnosis. Use the same reviewed request for apply.

Keep `config/orchestration.json`'s `program_worker_concurrency` at two. Native configured
capacity can further reduce this bound, never increase it. Fixed program claims share a
Vault mutex and recheck running native claims immediately before their ordinary claim CAS.
Each fixed semantic worker calls the model sequentially, including per-page writing.
The global reducer remains a single node. Other workflow consumers must be isolated during
scoped deployment/observation; the upper bound is not permission to dispatch another stage.

Use the existing operator control cancellation with one stable operation ID at the bounded
deadline. Verify cancelled workflow/batch, no pending journal, actual native workers and
reconciler stopped, no live leases and no runnable/triage native cards. Keep window costs
separate from drain costs. A naturally reached `checkpoint_1` pause stays paused and must
not be cancelled merely to obtain a stop receipt.

If a resource/global reducer ended without a domain result, select its observed node,
old native task ID and current failure file SHA. With explicit recovery authorization:

```json
{
  "workflow_id": "<observed-workflow>",
  "actor": "<observed-actor>",
  "expected_revision": 0,
  "input_digest": "<computed-mutation-digest>",
  "recovery_id": "<stable-recovery-id>",
  "recovery_scope": "reduce-cancel/v1",
  "reason": "<authorized interrupted-attempt recovery>",
  "entries": [{"node": "<resource-reduce:resource-id-or-global-reduce>",
               "task_id": "<observed-native-task>", "failure_sha256": "<current-failure-file-sha256>"}]
}
```

```bash
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" recover-reduce-execution --dry-run --request "<request.json>"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" recover-reduce-execution --request "<request.json>"
```

Dry-run writes no recovery/domain record. Applying the same request preserves all completed
reductions, Passes, reading packages, task ownership, gaps and pause evidence. The selected
failure and ended attempt are recorded; a later separately authorized resume binds a fresh
native card. The old binding never regains write authority. This supports only a released,
cancelled Reduce stage with `NATIVE_WORKER_STOPPED` or `WORKFLOW_STOPPED`, one recovery per
exact ended native attempt. A later independently cancelled attempt needs its own current
failure hash, ended run and explicit operator request; no automatic loop is added.
It cannot authorize semantic corrections, budget changes, source resets or retries
of unchanged refusals; another failure requires diagnosis.

An explicitly reviewed old source hold may instead use `source-hold/v1` with
`target_contract_sha256` from `source_hold_recovery.contract_hash()`. Each entry also
pins its original `snapshot_ref`/`snapshot_sha256`. A semantic refusal must pin its
`native_audit_ref`/`native_audit_sha256`; a transport failure must pin its native
`transport_session_id`, `transport_user_sha256` and `transport_response_sha256`.
The program authenticates the ended actual run, unchanged canonical binding (even
when an accepted supplement narrowed task_map), current source identity, immutable
snapshot, native transcript and either exact refused packet or proven connection
failure. Only one such recovery per source is admitted; no automatic retry follows.
The refused packet receives a measured material-presence index and explicit
permission to omit only unverifiable candidates with a coverage-loss reason. It
does not supply facts, repair OCR or approve content. Native source omissions and
their reasons remain in the formal reduction; other candidates stay eligible.

For this explicit migration only, a changed batch-wide excluded-task counter is
historical request metadata when all source tasks, hashes, candidates, original
materials, QA and the warning text still match exactly. The original full request
view is retained, allowing exact genuine packet replay. Current loss reports remain
in the backend and global input; this does not remove or reduce current coverage
loss. Any source input change rejects recovery/replay. Previously successful packets
remain byte-identical; only the authenticated refused packet gains clarification.
Refused responses are never reused. No durable pause is released by this operation.

New corrections use `source-hold/v2` with the same identity-bound entry shape and
one explicit recovery per source under this disposition contract. It distinguishes
an unsupported candidate from a request-integrity failure: an omission is an
unverified coverage loss, not verification, and does not need an earlier QA flag.
Missing required material objects, stale evidence, permissions and feeding defects
still block. The native model must inspect the originals, retain verifiable claims,
and give concrete loss reasons; grouping difficulty or budget is not a loss reason.
Exact successful base packets are replayed first. Only an unfinished packet without
a matching success receives measured presence/disposition framing, with one actual
model call and unchanged size limits. Legacy refusal framing is authenticated against
its prior authorized packet and exact material index; every candidate/evidence field,
original user bytes, source snapshot, schema, images and native response remain checked.

`source-hold/v3` additionally disambiguates earlier inspection narratives from source
material. QA status/notes and real source limits remain intact; a narrative is neither
another required asset nor evidence supporting a claim. Several valid support refs
are allowed without a one-material-per-subclaim rule. Exact v1/base and v2 successful
calls are tried before the new framing, preserving supported partial outcomes. Native
program framing from a v2 source-wide grant is authenticated against the complete
unchanged source packet plan, not only its first originally refused packet.

The source packet executor isolates an audited local semantic/schema/coverage refusal:
it marks that call unvalidated, records held candidate handles and continues each later
packet once. It does not retry the refused packet. Permission, identity, source and
budget failures stop execution. With any held packet, no source reduction is submitted
and no worker completion is claimed; `source-packets-deferred/v1` records successful
audit refs and the finite unresolved list. Full-source coverage remains mandatory.

Before a model call the current worker may replay an existing genuine native audit only for
the same node, exact system/user/schema/image hashes, pinned template and authorized old
ended run. Native session source, transcript and response hashes are checked. Rejected,
refused, unrelated and changed calls are not replayed. This includes already returned
global plans and page bodies not yet submitted when interrupted; no inferred missing page
is created. Every recovered draft still passes fresh normal source/ACL/coverage and commit
checks. A call stopped before its response was durably audited has no reusable result.
Completed domain results use trusted reconciliation instead of another semantic call.

## Reviewed packet correction before global consumption

`recover-reduce-execution` scope `source-result-review/v1` is the explicit exception
for a completed source result with reviewed semantic/omission defects. It requires
a cancelled Reduce workflow, no live consumers, unchanged source/Pass authority,
an active reduction not consumed by any global/build run, its `reduction_id` and
`reduction_sha256`, original `snapshot_ref`/`snapshot_sha256`, and selected completed
packet `selected_audits` (`ref`, `sha256`). Pin `target_contract_sha256` from
`source_result_review.contract_hash()` and a substantive operator review reason.

Admission authenticates every packet's original ended native session, exact wire,
schema, images and response, and reconstructs the current formal source result.
Only selected complete packets gain one new native review; all others use the
existing exact replay verifier, indexed by authenticated audit references. Review
requires whole-candidate omission for unsupported required facts/values; a summary
caveat is not validation, and inability to group is not an omission reason.

The old record remains immutable. Only the current authorized native worker can
append a replacement with `supersedes_reduction_id`, preserve every unselected
packet, and atomically update the active source pointer under the existing locks.
This does not revise Passes or reset sources. Global input identity changes through
the ordinary graph; old global results are reused only if their inputs still match.
No semantic refusal is relabeled as cancellation and no pause is released. One
review grant per source is allowed; failure is held rather than automatically retried.

## Shared finite output correction

`finite-semantic-output/v1` applies the existing pure schema/packet validators to
resource/global packets, detail, page writing and page-review JSON. A persistent
`output-attempts` record permits the initial call and one diagnostic correction.
It never turns a semantic refusal, QA failure, changed authority or transport error
into correction. The full original packet, preceding state, schema and correction
are measured again; nothing is truncated. Unknown native completion remains held.
Valid native results are revalidated and reused before any new call. Prompt,
worker and process changes do not reset consumed allowance.

Diagnostics include the failed answer as data, all independently computable
coverage/overlap findings, concrete conflicting paths and exact offered append
identities (including aliases). The model returns a complete replacement; the
program never patches domain choices. Full validation remains authoritative.
Global diagnostics inspect the same complete accumulated planning state as
acceptance, even when an earlier coverage error was reported first. Only actual
identity/path collisions appear as `reserved_targets`, with output indices and
field differences. These metadata are explicitly not offered append targets:
the model must choose a distinct valid identity/path or a permitted detail
request. No body, unrelated directory or membership is added, and no semantic
equivalence or merge authority is inferred. Normal execution and recovery use
the same state-aware diagnostic; saved native requests and attempt counts remain
unchanged.
Persisted diagnostics are part of their original native wire and remain frozen
when diagnostic code changes; successful old corrections still replay exactly.
Repeated targets are shared once, findings address failed-response page indices,
and a many-way path collision is one group; complete evidence and the failed
answer are never truncated. A diagnostic blocked by request-size preflight before
reservation has not consumed the second call. The same recovery entry can prove
its exact one-call journal, original rejected transcript and previously oversized
diagnostic, then measure the corrected diagnostic and recover that unused second
ordinal. This cannot recover source-budget failures, a sent/uncertain correction,
or a two-call exhausted record. Previously sent diagnostics remain frozen; an
unused slot may use newly measured diagnostics without changing the first record.

For an explicitly authorized legacy incremental global failure, use the existing
`recover-reduce-execution` with `recovery_scope: semantic-output/v1`, the current
`target_contract_sha256` from `semantic_output_recovery.contract_hash()`, and each
entry's usual node/task/failure SHA plus `snapshot_ref/snapshot_sha256`,
`progress_ref/progress_sha256`, and `native_audit_ref/native_audit_sha256`.
Dry-run authenticates the ended native transcript and reconstructs the complete
prior planning state from successful native calls, then reproduces the failure at
the same pure validator. Apply imports that call as attempt one, without resuming.
Normal execution and this admission use the same validator, diagnosis and journal.
This is a provenance migration for old records, not an extra allowance or an
error-specific repair endpoint; legacy formats without provable state remain held.

A reservation with no SDK session and no response audit can be resumed only after
the same entry authenticates its ended run, exact journal/diagnostic and valid
prefix. Preserve the old not-called receipt and continue the same second ordinal;
do not mint a third attempt. Admission and canonical replay share
`execution_recovery_key`; both use the exact ended native identity for this scope.
Uncertain SDK completion, permission or stale-input failures never use this proof.

For an explicitly authorized continuation after a confirmed diagnostic defect,
use this same `semantic-output/v1` entry, not a new recovery scope. Supply
`continue_exhausted: true`, the exact `attempt_ref/attempt_sha256` and
`reviewed_diagnostic_sha256`, alongside the existing source/progress/failure and
last-native-audit hashes. Admission reconstructs the valid prefix and authenticates
both rejected native wires, including the original correction. It requires newly
precise mechanical page findings, unchanged evidence/schema and a measured full
request within budget. Material refusals, uncertain completion and changed input
remain ineligible. Apply records an `exhausted_continuation` in the existing
recovery report; resume is still separate. The same journal retains attempts 1
and 2, then records exactly one explicitly authorized call as ordinal 3, with no
automatic correction of that call. A prior continuation or three consumed calls
cannot obtain another grant. Prompt changes and process recovery do not authorize
this exception. The default automatic limit remains two calls.

Page review retains one substantive check. Only concrete factual, numeric/unit,
logic, scope, condition/exception, conflict, necessary-coverage or required-parent
content defects justify revision; style/layout/polish does not. A legal revision
decision stays held, not automatically corrected into approval. No dedicated
review cache or new finalization boundary is introduced.

### Interrupt and resume without resetting output attempts

Use the existing `cancel` operation for an immediate stop. A response that
finishes during cancellation is saved and checked; cancellation never permits
starting its correction. This is an interrupted continuation, not an additional
semantic failure or a new allowance. After drain, `interruption-status` (request:
`workflow_id`, `actor`) reports saved fixed-output journals and distinguishes
reusable results, an unused standard correction, exhausted/held output and
uncertain native completion. This read-only inventory grants no execution rights.
Preparation/Pass and other stages retain their existing stage-specific evidence
and supported recovery checks; this inventory does not migrate their contracts.

For a cancelled fixed Reduce worker, use `recover-reduce-execution` with the
existing `reduce-cancel/v1` scope, exact ended native identity/failure hash and a
separate authorized `resume`. Optionally add `stop_after_output` to that node's
entry, containing `attempt_ref` and `attempt_sha256`. It binds the existing journal
and preserves its original attempts. Only saved responses may replay before the
selected output; only that output may make a remaining native call. The original
two-call limit, full native/input/schema validation and evidence refusals remain
unchanged. An uncertain reservation or exhausted rejection cannot obtain another
call by using this limit. No correction is sent after a hard cancellation.

After the selected output passes, the caller saves its packet progress (including
pending detail units), writes an `output-boundary-<recovery_id>.json` receipt and
ends the worker with `WORKFLOW_STOPPED`. The trusted operator/supervisor then uses
the existing stable `cancel` and drain verification, leaving subsequent packets,
detail units and downstream stages stopped. A later explicitly authorized cancel
recovery of that node replaces this execution limit; restarting or plain resume
does not erase it. This shared limit covers source grouping, global plan/detail
and page-writing outputs; it is not a promise to resume an unknown native call or
to cross a durable stage pause.
