# global-reduce · v4

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: aggregate the batch's completed resource reductions once.
- Allowed inputs: pinned batch ID, complete resource reduction IDs and their summaries/candidate references; no raw reading packages.
- Allowed commands: controlled-ingest `manage_knowledge_build.py batch-global-reduce`.
- Fixed IDs / hashes: workflow, batch, reduction ID set and node fingerprint; verify all resources have exactly one valid reduction.
- Bounds: one batch global reduction; no new Pass or resource reduction.
- Heartbeat: Kanban heartbeat; fail on changed resource set.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"global_reduction_id":string|null,"draft_run_ids":string[],"error_code":string|null}`.
- Complete when the global request owns stable identity, output path and draft-run assignment.
- Prohibited: direct legacy Reduce for a new plan, Build Finalize, release or checkpoint approval.
- Call `dispatch_ingest_workflow.py worker-begin` before the global reduction. After its durable draft runs exist call `worker-complete` with the returned template hash; on failure call `worker-fail`. The adapter validates the authoritative reduction before dispatching checkpoint 1.
