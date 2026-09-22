# global-reduce · v1

- Role / sole objective: aggregate the batch's completed resource reductions once.
- Allowed inputs: pinned batch ID, complete resource reduction IDs and their summaries/candidate references; no raw reading packages.
- Allowed commands: controlled-ingest `manage_knowledge_build.py batch-global-reduce`.
- Fixed IDs / hashes: workflow, batch, reduction ID set and node fingerprint; verify all resources have exactly one valid reduction.
- Bounds: one batch global reduction; no new Pass or resource reduction.
- Heartbeat: Kanban heartbeat; fail on changed resource set.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"global_reduction_id":string|null,"draft_run_ids":string[],"error_code":string|null}`.
- Complete when the global request owns stable identity, output path and draft-run assignment.
- Prohibited: direct legacy Reduce for a new plan, Build Finalize, release or checkpoint approval.
