# build-finalize · v1

- Role / sole objective: finalize all approved draft runs in the pinned batch, serially.
- Allowed inputs: approved checkpoint-1 record, batch/draft-run IDs, reductions and validation evidence.
- Allowed commands: controlled-ingest `manage_knowledge_build.py` Build Finalize/status commands for pinned runs only.
- Fixed IDs / hashes: workflow, batch, draft-run IDs, checkpoint approval digest and node fingerprint; verify revisions.
- Bounds: one run at a time, only runs in this batch; do not parallelize final writes.
- Heartbeat: Kanban heartbeat between runs; stop on changed approval/input.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"completed_run_ids":string[],"failed_run_ids":string[],"error_code":string|null}`.
- Complete when every eligible run has durable completed state or an explicit failure is reported.
- Prohibited: new Pass/Reduce, release apply, Provider sync or checkpoint-2 approval.
