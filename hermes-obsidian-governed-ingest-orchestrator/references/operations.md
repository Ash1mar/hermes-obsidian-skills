# Operations

All examples use `python3 "<skill-dir>/scripts/<script>.py" --vault "<vault>" <command>`. Request-taking commands read JSON from `--request "<file>"`; never embed secrets in CLI arguments.

| Intent | Command | Result |
| --- | --- | --- |
| Start once | `dispatch_ingest_workflow.py start` | Vault record + pinned templates; attempts Kanban projection |
| Inspect | `manage_ingest_workflow.py status --workflow-id <id> --compact` | Authoritative stage, revision, checkpoints |
| Preview canary | `manage_ingest_workflow.py canary-preview --workflow-id <id> --limit 8` | Read-only first eight ready slice IDs and selection digest |
| Arm canary | `dispatch_ingest_workflow.py arm-canary` | Persist the exact previewed allowlist and project only those Pass cards |
| Stop canary | `dispatch_ingest_workflow.py disarm-canary` | Persist disabled policy; subsequent worker checks fail closed |
| Resume | `dispatch_ingest_workflow.py resume` | Clears permitted stop, then resynchronizes projection |
| Approve | `manage_ingest_workflow.py approve` | Records named human checkpoint with approval digest |
| Cancel | `dispatch_ingest_workflow.py cancel` | Persists cancellation and blocks projected cards |
| Recover board | `manage_ingest_workflow.py rebuild-kanban`, then `dispatch_ingest_workflow.py sync` | Rebuilds projection from Vault |

Before any mutation inspect current status and compute a fresh digest with `manage_ingest_workflow.py digest`. Gateway/dispatcher failure after start is a recoverable result: `workflow_created: true`, `state: dispatcher_unavailable`, `background_dispatch: false`. Do not fall back to a long conversational run. An approval is human-originated; never synthesize it from worker output. Keep both checkpoint gates blocked until their respective approval records are valid.

Current `worker_dispatch_enabled: false` is the rollout gate. Operators may inspect cards and template snapshots, but must not interpret them as running workers. The host may enable the flag only after synthetic tests and exact-version deployment checks. The Vault canary allowlist remains a second, independent gate: unarmed workflows dispatch no worker, and armed workflows dispatch only the previewed Pass slices, never reducers or finalizers.
