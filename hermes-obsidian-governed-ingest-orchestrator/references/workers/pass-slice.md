# pass-slice · v1

- Role / sole objective: finish exactly the named Vault slice with ordinary Pass evidence.
- Allowed inputs: only the claimed slice's task snapshots and listed bounded reading packages/UnitRefs; open no other batch readings.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, `worker-check`, `worker-heartbeat`, `worker-complete` or `worker-fail`; controlled-ingest `manage_knowledge_build.py batch-pass` for each leased task.
- Fixed IDs / hashes: workflow ID, Kanban task ID, batch ID, slice ID, task IDs, slice input fingerprint, pinned slice template ID/hash, lease worker ID and expected revision must match on every write.
- Bounds: one slice; obey exact reading windows and model budget; finish the current slice then exit.
- Heartbeat: maintain both Vault `slice-heartbeat` and Kanban heartbeat before lease expiry; no work without a Vault lease.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"slice_id":string,"pass_ids":string[],"lease_revision":integer,"error_code":string|null}`.
- Complete only after initial and subsequent Pass records are durable, `worker-complete` verifies them and the Kanban task can be marked done. On invalid model JSON or other failure call `worker-fail` with the raw failed output.
- Prohibited: plan, prepare, Reduce, Finalize, release, approval, scanning the whole batch or changing the slice-pinned template hash.
