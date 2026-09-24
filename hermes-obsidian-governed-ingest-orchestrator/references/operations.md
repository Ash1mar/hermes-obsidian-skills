# Operations

All examples use `python3 "<skill-dir>/scripts/<script>.py" --vault "<vault>" <command>`. Request-taking commands read JSON from `--request "<file>"`; never embed secrets in CLI arguments.

| Intent | Command | Result |
| --- | --- | --- |
| Start once | `dispatch_ingest_workflow.py start` | Vault record + pinned templates; attempts Kanban projection |
| Inspect | `manage_ingest_workflow.py status --workflow-id <id> --compact` | Authoritative stage, revision, checkpoints |
| Preview canary | `manage_ingest_workflow.py canary-preview --workflow-id <id> --limit 8` | Read-only first eight ready slice IDs and selection digest |
| Arm canary | `dispatch_ingest_workflow.py arm-canary` | Persist the exact previewed allowlist and project only those Pass cards |
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

## Auditable pre-batch repair

`python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" repair-preparation --request "<request.json>"` requires a cancelled workflow with no batch. Include workflow_id, actor, expected_revision, input_digest, a stable repair_id, reason, existing Vault evidence_refs, complete templates from the installed orchestration.worker_pack(), and optional execution_mode. Each reset_sources entry must pin path and outcome_digest (`sha256:` plus fingerprint of the current failed outcome). Only those failed outcomes are reset; valid registrations and other source outcomes remain. The before.json snapshot, its hash and repair history retain the old record. Old template snapshots stay immutable and old cards lose their binding. The command is idempotent for the same repair ID and digest, and does not resume work. A subsequent normal resume projects new cards from the repaired contracts.

For a bounded trial choose canary_only. All requested sources are prepared (real failed files remain explicit gaps), exact planning runs, and exactly eight ready Pass slices run. The dispatcher writes reports/canary.json after their durable completion, then stops eligibility; no automatic promotion, Reduce or release follows. Fewer than eight ready slices reports canary_unavailable. Use auto_full only for an explicitly requested full run.

worker-register-source is the standard source identity allocator. Runtime/contract errors create a Vault failure report without writing a failed-source outcome; the trusted reconciler blocks the card. Domain governance commits and SourceUnit publication use an explicit `--worker-binding` request with workflow_id, task_id and node, verified under the same workflow lock as cancellation. Successful cancellation therefore prevents subsequent authoritative source commits even when Hermes strips task environment variables; in-flight conversion scratch output is not a committed artifact. Worker commands never call the Kanban CLI. These cooperative worker guards are not an OS security sandbox.
