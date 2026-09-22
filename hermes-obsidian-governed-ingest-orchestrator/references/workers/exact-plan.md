# exact-plan · v1

- Role / sole objective: create or adopt one exact-reading-budget knowledge-build batch for this workflow scope.
- Allowed inputs: pinned source artifact IDs, UnitSet IDs, token audit, requested task scope and current registry revision.
- Allowed commands: read-only measure/preview and `manage_knowledge_build.py batch-plan --exact-reading-budget` with the pinned request.
- Fixed IDs / hashes: verify workflow ID, source/UnitSet fingerprints, planner request digest and expected registry revision.
- Bounds: one batch, explicit task list, measured reading budget; no speculative extra tasks.
- Heartbeat: Kanban heartbeat while measuring; fail if pinned source/registry changes.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"batch_id":string|null,"task_ids":string[],"plan_fingerprint":string|null,"error_code":string|null}`.
- Complete when the exact plan and task IDs are persisted and bound to the workflow.
- Prohibited: model Pass, Reduce, Finalize, release, checkpoint approval or estimating away over-budget diagnostics.
