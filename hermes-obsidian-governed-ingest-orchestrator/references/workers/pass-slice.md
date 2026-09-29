# pass-slice · v7

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. Read the canonical JSON at the card's `worker_binding` path and copy it to one new workspace request file. Keep its workflow/node/task/hash identity unchanged. Call worker-begin before any worker-check. For a Pass slice the binding already provides worker_id and the slice template_hash; add only the lease revision returned by begin. Never copy worker_template_hash or reconstruct identity from environment variables.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: finish exactly the named Vault slice with ordinary Pass evidence.
- Allowed inputs: only the claimed slice's task snapshots and listed bounded reading packages/UnitRefs; open no other batch readings.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, `worker-check`, `worker-heartbeat`, `worker-complete` or `worker-fail`; controlled-ingest `manage_knowledge_build.py batch-pass` for each leased task.
- Persist semantic Pass JSON with `python3 "<domain_script>" --vault "<vault>" --worker-binding "<workspace binding request>" batch-pass --request "<Pass JSON>"`. Use the absolute domain_script from the card and the binding's actor. The binding flag is required in the isolated terminal. Read only the returned slice's task snapshots and bounded package files. Record candidate sequence 0 and citation sequence 1 against current task revisions; an empty candidate set still requires a grounded inspection and an explicit empty_reason. The model performs these inspections; never synthesize completion from an empty template.
- Fixed IDs / hashes: workflow ID, Kanban task ID, batch ID, slice ID, task IDs, slice input fingerprint, pinned slice template ID/hash, lease worker ID and expected revision must match on every write.
- Bounds: one slice; obey exact reading windows and model budget; finish the current slice then exit.
- Heartbeat: maintain both Vault `slice-heartbeat` and Kanban heartbeat before lease expiry; no work without a Vault lease.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"slice_id":string,"pass_ids":string[],"lease_revision":integer,"error_code":string|null}`.
- Complete only after initial and subsequent Pass records are durable, `worker-complete` verifies them and the Kanban task can be marked done. On invalid model JSON or other failure call `worker-fail` with the raw failed output.
- Prohibited: plan, prepare, Reduce, Finalize, release, approval, scanning the whole batch or changing the slice-pinned template hash.
- In `auto_full`, the dispatcher arms exactly eight initial slices, promotes the remaining slices only after those eight have valid durable Pass results, and continues to Reduce after every slice completes. A worker still processes only its assigned slice.
