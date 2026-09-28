# Operations

All examples use `python3 "<skill-dir>/scripts/<script>.py" --vault "<vault>" <command>`. Request-taking commands read JSON from `--request "<file>"`; never embed secrets in CLI arguments.

| Intent | Command | Result |
| --- | --- | --- |
| Start once | `dispatch_ingest_workflow.py start` | Vault record + pinned templates; attempts Kanban projection |
| Inspect | `manage_ingest_workflow.py status --workflow-id <id> --compact --runtime` | Authoritative stage, revision, checkpoints plus read-only native attempts |
| Preview canary | `manage_ingest_workflow.py canary-preview --workflow-id <id> --limit 8` | Read-only first eight ready slice IDs and selection digest |
| Arm canary | `dispatch_ingest_workflow.py arm-canary` | Persist the exact previewed allowlist and project only those Pass cards |
| Prepare one source (v5) | `dispatch_ingest_workflow.py worker-prepare-source` | Bound registration, supervised PDF conversion, QA and SourceUnit publication; preserves pending on runtime/review errors |
| Source worker result | `dispatch_ingest_workflow.py worker-complete` or `worker-fail` | Commit one validated ready UnitSet or one typed failed-source coverage gap; continue other source cards |
| Exact plan worker result | `dispatch_ingest_workflow.py worker-complete` | Adopt the exact-budget batch after every source has a result; only ready UnitSets may enter the batch |
| Stop canary | `dispatch_ingest_workflow.py disarm-canary` | Persist disabled policy; subsequent worker checks fail closed |
| Resume | `dispatch_ingest_workflow.py resume` | Clears permitted stop, then resynchronizes projection |
| Approve in manual mode | `manage_ingest_workflow.py approve` | Records named human checkpoint with approval digest |
| Cancel | `dispatch_ingest_workflow.py cancel` | Persists cancellation and blocks projected cards |
| Recover board | `manage_ingest_workflow.py rebuild-kanban`, then `dispatch_ingest_workflow.py sync` | Rebuilds projection from Vault |
| Recover reconciler | `watch_ingest_workflow.py --workflow-id <id>` from a trusted non-worker terminal | Continues Kanban acknowledgement and successor dispatch from Vault outcomes |

Before any mutation inspect current status and compute a fresh digest with `manage_ingest_workflow.py digest`. Gateway/dispatcher failure after start is a recoverable result: `workflow_created: true`, `state: dispatcher_unavailable`, `background_dispatch: false`. Do not fall back to a long conversational run. In `auto_full`, each checkpoint worker validates the exact Vault state, writes a result report, and calls `worker-complete`; the adapter writes a hashed decision and the workflow service independently verifies it. Passing decisions automatically advance the gate. Failed validation leaves the card blocked and exposes report and blocking codes. Manual mode still accepts explicit human approval.

Current `worker_dispatch_enabled: true` permits scoped source workers and exact planning after coverage checks. In `auto_full`, the first eight ready slices form a bounded canary; once all eight have durable valid Pass records, remaining slices and the downstream workers become eligible in order. Manual mode retains explicit canary arm and approval operations. Inspect compact status for `source_coverage` before planning or reporting completion. Failed sources are listed as gaps and require a partial final workflow outcome.

## Workflow lock recovery

New workflow, dispatch and watcher locks use host kernel locks. Their stable kernel files live
outside the Vault (`~/.cache/hermes-skill-runtime/locks` on Linux; the user temporary directory
on Windows). Vault owner markers describe the holder but do not determine occupancy.
Process exit, SIGTERM, SIGKILL or OOM releases the kernel lock; the next local invocation
can replace a leftover marker. Never delete kernel lock files while processes may be running.
`LOCK_BUSY` means actual contention; `REVISION_CONFLICT` means a ledger revision conflict.
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

New workflows pin pack v5. `worker-prepare-source` takes workflow_id, node and task_id;
its begin call supplies the pinned template hash internally. It makes no agent chat-model calls and never
calls the Kanban CLI. Local conversion retains the shared host gate and cancellation monitoring.
Only this workflow/source's deterministic attempt directories are automatically resumed; use
`bundle` for explicitly requested existing Bundle reuse. Already current units are validated
against the assigned raw hash and identity before reuse. A validation failure allows one supported
pipeline retry with both attempts preserved. Missing output or runtime/config errors stop for review;
they are not automatically recorded as damaged PDF. On a failed command, obtain `worker-begin`'s
hash if needed for `worker-fail`, then report the actual code while binding checks still pass.
Completing one source does not enable exact planning/Canary until the remaining sources finish.
Changing installed templates never updates an existing workflow's pinned pack automatically.
