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
| Approve | `manage_ingest_workflow.py approve` | Records named human checkpoint with approval digest |
| Cancel | `dispatch_ingest_workflow.py cancel` | Persists cancellation and blocks projected cards |
| Recover board | `manage_ingest_workflow.py rebuild-kanban`, then `dispatch_ingest_workflow.py sync` | Rebuilds projection from Vault |

Before any mutation inspect current status and compute a fresh digest with `manage_ingest_workflow.py digest`. Gateway/dispatcher failure after start is a recoverable result: `workflow_created: true`, `state: dispatcher_unavailable`, `background_dispatch: false`. Do not fall back to a long conversational run. An approval is human-originated; never synthesize it from worker output. Keep both checkpoint gates blocked until their respective approval records are valid.

Current `worker_dispatch_enabled: true` permits scoped source workers and the exact-plan worker after coverage checks. The Vault canary allowlist remains a separate Pass gate: unarmed workflows dispatch no Pass worker, and armed workflows dispatch only the previewed Pass slices, never reducers or finalizers. Inspect compact status for `source_coverage` before planning or reporting completion. Failed sources are listed as gaps and require a partial final workflow outcome.
