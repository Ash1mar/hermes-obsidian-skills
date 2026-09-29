# exact-plan · v6

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: create or adopt one exact-reading-budget knowledge-build batch for this workflow scope.
- Allowed inputs: pinned source artifact IDs, UnitSet IDs, token audit, requested task scope and current registry revision.
- Execute `python3 "<dispatcher_script>" --vault "<vault>" worker-plan-exact --request "<request.json>"` using terminal `background=true, notify=true`. Copy workflow_id/node/task_id from this card. Poll the returned process session in short calls and keep worker heartbeats active; foreground terminal calls are subject to Hermes's outer 420-second deadline. Start only one process per card, and confirm its exit before retrying.
- The helper validates ready UnitSets and live registry/ACL, splits candidate groups by actual serialized budget, atomically saves measurements and progress under `_system/reports/exact-planning/`, creates the exact batch using verified measurement checkpoints and commits worker-complete. On interruption rerun the same helper with the same IDs; matching measurements are reused after revalidation. Do not generate an ad hoc fixed-size planner or run a second full batch measurement.
- An indivisible oversize unit produces a typed blocked progress report. Preserve it for review; never estimate away the reading budget.
- Fixed IDs / hashes: verify workflow ID, source/UnitSet fingerprints, planner request digest and expected registry revision.
- Bounds: one batch, explicit task list, measured reading budget; no speculative extra tasks.
- Heartbeat: Kanban heartbeat while measuring; fail if pinned source/registry changes.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"batch_id":string|null,"task_ids":string[],"plan_fingerprint":string|null,"error_code":string|null}`.
- Complete when the exact plan and task IDs are persisted and bound to the workflow.
- Prohibited: model Pass, Reduce, Finalize, release, checkpoint approval or estimating away over-budget diagnostics.
- The helper performs worker-begin and worker-complete. Every scoped source must have a durable result and at least one must be ready. It plans the full ready scope, retains failed paths/hashes/codes/reasons as explicit coverage gaps, and respects the fixed batch ID in the scope. No source ready means stop without a batch.
