# Phase-6 Hermes Kanban adapter

`scripts/dispatch_ingest_workflow.py` is the optional projection of an
authoritative Vault ingest workflow onto Hermes Kanban. Run it in the Hermes
Linux environment. It uses the installed `hermes kanban` CLI with a dedicated,
deterministic board slug; it never switches the current board and never reads
Kanban status back into the Vault as workflow truth.

The adapter supports `start`, `sync`, `cancel`, `resume`, `worker-begin`,
`worker-check`, `worker-heartbeat`, `worker-complete`, and `worker-fail` with a
JSON `--request`. `start` takes the phase-5 start request. `sync`, `cancel`,
and `resume` take `workflow_id`, `actor`, `expected_revision`, and
`input_digest` as defined in `workflow-ledger.md`. Worker requests identify
the bound Kanban `task_id`, exact `pass-slice:<slice-id>` node, and a unique
`worker_id`; checks/heartbeat/completion also carry slice `expected_revision`
and `template_hash`.

Graph projection:

```text
source prepare per source → exact plan
Pass slices → resource reducers → global reducer → checkpoint-1 validator
→ human gate → serial Build Finalize → Vault Finalize plan
→ checkpoint-2 validator → human gate → release apply
→ optional Provider sync → acceptance
```

For an adopted batch, source preparation and planning are already represented
by its pinned batch and are not enqueued again. Slices are initialized without
issuing a lease. Kanban nodes use stable keys
`ingest:<workflow-id>:<node-kind>:<input-fingerprint>` and explicit parent
dependencies. Re-running `sync` reuses these keys and marks nodes done only
when the Vault domain records say they are done. If Kanban SQLite is lost,
`sync` reconstructs the board and tasks from the Vault. A stale Kanban node
cannot acquire a Vault slice lease because `worker-begin` verifies its binding,
input fingerprint, and active workflow stage. Batch-level Pass concurrency
and cooldown remain authoritative. `sync` reclaims expired Vault leases and
promotes elapsed retry windows before aligning Kanban scheduled/blocked cards.

The two gate cards are never worker-executable; the adapter completes them
only after the matching Vault checkpoint is approved. `cancel` stops the
workflow and batch leases, blocking remaining Kanban cards. `resume` explicitly
reopens cancelled slices; the worker must re-check the pinned input before
processing. A worker must call `worker-check` before model work and before
`batch-pass`, heartbeat both the Vault lease and Kanban task, commit Passes
through `batch-pass` with slice ID/template hash/worker ID, then call
`worker-complete`. The latter verifies candidate and citation Passes in the
Vault before completing the slice and finally the Kanban card. Domain results
are written before Kanban completion so a crash is recoverable by `sync`.

If the gateway is absent, `start` still creates the workflow and returns
`state: dispatcher_unavailable`, `workflow_created: true`, and
`background_dispatch: false`. In phase 6, fixed worker templates are not yet
installed, so the production CLI creates all projected worker cards blocked
even when the gateway is running and reports `background_dispatch: false`.
Phase 7 supplies and hashes those templates before enabling execution. Do not
run a long interactive fallback or claim that blocked cards are running.
