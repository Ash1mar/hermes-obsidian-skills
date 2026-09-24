# pass-slice · v4

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: finish exactly the named Vault slice with ordinary Pass evidence.
- Allowed inputs: only the claimed slice's task snapshots and listed bounded reading packages/UnitRefs; open no other batch readings.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, `worker-check`, `worker-heartbeat`, `worker-complete` or `worker-fail`; controlled-ingest `manage_knowledge_build.py batch-pass` for each leased task.
- Fixed IDs / hashes: workflow ID, Kanban task ID, batch ID, slice ID, task IDs, slice input fingerprint, pinned slice template ID/hash, lease worker ID and expected revision must match on every write.
- Bounds: one slice; obey exact reading windows and model budget; finish the current slice then exit.
- Heartbeat: maintain both Vault `slice-heartbeat` and Kanban heartbeat before lease expiry; no work without a Vault lease.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"slice_id":string,"pass_ids":string[],"lease_revision":integer,"error_code":string|null}`.
- Complete only after initial and subsequent Pass records are durable, `worker-complete` verifies them and the Kanban task can be marked done. On invalid model JSON or other failure call `worker-fail` with the raw failed output.
- Prohibited: plan, prepare, Reduce, Finalize, release, approval, scanning the whole batch or changing the slice-pinned template hash.
- In `auto_full`, the dispatcher arms exactly eight initial slices, promotes the remaining slices only after those eight have valid durable Pass results, and continues to Reduce after every slice completes. A worker still processes only its assigned slice.
