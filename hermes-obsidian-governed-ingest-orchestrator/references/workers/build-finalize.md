# build-finalize · v3

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card / `HERMES_KANBAN_TASK`; after begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: finalize all approved draft runs in the pinned batch, serially.
- Allowed inputs: approved checkpoint-1 record, batch/draft-run IDs, reductions and validation evidence.
- Allowed commands: controlled-ingest `manage_knowledge_build.py` Build Finalize/status commands for pinned runs only.
- Fixed IDs / hashes: workflow, batch, draft-run IDs, checkpoint approval digest and node fingerprint; verify revisions.
- Bounds: one run at a time, only runs in this batch; do not parallelize final writes.
- Heartbeat: Kanban heartbeat between runs; stop on changed approval/input.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"completed_run_ids":string[],"failed_run_ids":string[],"error_code":string|null}`.
- Complete when every eligible run has durable completed state or an explicit failure is reported.
- Prohibited: new Pass/Reduce, release apply, Provider sync or checkpoint-2 approval.
- Call `dispatch_ingest_workflow.py worker-begin` first. Finalize all pinned runs serially with recorded page reviews, then call `worker-complete` with the returned template hash. On failure call `worker-fail`. The adapter checks every run is completed before opening release planning.
