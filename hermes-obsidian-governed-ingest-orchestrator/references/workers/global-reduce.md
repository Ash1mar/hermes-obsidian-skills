# global-reduce

- `WORKFLOW_PAUSED` is a durable operator boundary. Stop without bypassing it, requesting continuation, cancelling the workflow or recording a failed source. A boundary owner with an already durable result may submit only `worker-complete`; the trusted reconciler acknowledges that result while holding successors.

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. Read the canonical JSON at the card's `worker_binding` path and copy it to one new workspace request file. Keep its workflow/node/task/hash identity unchanged. Call worker-begin before any worker-check. For a Pass slice the binding already provides worker_id and the slice template_hash; add only the lease revision returned by begin. Never copy worker_template_hash or reconstruct identity from environment variables.
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
