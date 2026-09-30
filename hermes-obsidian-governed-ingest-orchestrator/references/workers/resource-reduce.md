# resource-reduce

- `WORKFLOW_PAUSED` is a durable operator boundary. Stop without bypassing it, requesting continuation, cancelling the workflow or recording a failed source. A boundary owner with an already durable result may submit only `worker-complete`; the trusted reconciler acknowledges that result while holding successors.

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. Read the canonical JSON at the card's `worker_binding` path and copy it to one new workspace request file. Keep its workflow/node/task/hash identity unchanged. Call worker-begin before any worker-check. For a Pass slice the binding already provides worker_id and the slice template_hash; add only the lease revision returned by begin. Never copy worker_template_hash or reconstruct identity from environment variables.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: reduce Pass results for exactly one resource.
- Allowed inputs: that resource's task snapshots, Pass/candidate references and pinned batch/resource IDs only; no concatenated reading packages.
- Allowed commands: controlled-ingest `manage_knowledge_build.py batch-resource-reduce` with an exact request.
- Fixed IDs / hashes: workflow, batch, resource, task IDs, Pass IDs and node fingerprint must match Vault; recheck revision before write.
- Bounds: one resource reduction, bounded evidence set; no other resources.
- Heartbeat: Kanban heartbeat during synthesis; stop if Pass coverage changes.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"resource_id":string,"reduction_id":string|null,"evidence_refs":string[],"error_code":string|null}`.
- Complete when the reduction record is durable and attributable to this resource.
- Prohibited: whole-batch reading, global Reduce, Finalize, release or checkpoint approval.
- Before the domain command call `dispatch_ingest_workflow.py worker-begin` for this card. After the exact resource reduction is durable call `worker-complete` with the returned template hash. On failure call `worker-fail` with a typed reason. The adapter checks the pinned resource outcome and dispatches eligible successors.
