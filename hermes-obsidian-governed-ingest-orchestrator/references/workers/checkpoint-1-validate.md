# checkpoint-1-validate · v3

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card / `HERMES_KANBAN_TASK`; after begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: validate the knowledge-build boundary and record a verifiable decision.
- Allowed inputs: pinned batch, Pass coverage, resource/global reductions, QA and validation reports.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, then `worker-complete` to run batch validation and persist the report and decision. `worker-fail` records a typed failure.
- Fixed IDs / hashes: workflow ID, batch ID, reduction IDs and node fingerprint; report current revisions.
- Bounds: one checkpoint-1 validation, no edits to knowledge outputs.
- Heartbeat: Kanban heartbeat while validation runs.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_1","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when passing validation is durable and the workflow service has rechecked and recorded the decision. A failed validation blocks this card with a report.
- Prohibited: constructing an approval digest, editing decision evidence, Build Finalize or bypassing QA. Manual-mode approval is separate.
