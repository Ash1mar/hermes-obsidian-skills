# exact-plan · v4

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: create or adopt one exact-reading-budget knowledge-build batch for this workflow scope.
- Allowed inputs: pinned source artifact IDs, UnitSet IDs, token audit, requested task scope and current registry revision.
- Allowed commands: read-only measure/preview and `manage_knowledge_build.py batch-plan --exact-reading-budget` with the pinned request.
- Fixed IDs / hashes: verify workflow ID, source/UnitSet fingerprints, planner request digest and expected registry revision.
- Bounds: one batch, explicit task list, measured reading budget; no speculative extra tasks.
- Heartbeat: Kanban heartbeat while measuring; fail if pinned source/registry changes.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"batch_id":string|null,"task_ids":string[],"plan_fingerprint":string|null,"error_code":string|null}`.
- Complete when the exact plan and task IDs are persisted and bound to the workflow.
- Prohibited: model Pass, Reduce, Finalize, release, checkpoint approval or estimating away over-budget diagnostics.
- Before planning, call `worker-begin` and inspect its `source_coverage`. Every scoped source must have a durable result and at least one must be ready. Build the exact plan only from validated current UnitSets named by ready outcomes; exclude failed sources and preserve their paths, hashes, error codes and reasons as explicit coverage gaps in the plan report. After `batch-plan --exact-reading-budget`, call `worker-complete` with the batch ID; the workflow rejects batches that reference a failed source or omit a ready source. If no source is ready, stop with a typed gap report rather than create an empty or unrelated batch.
