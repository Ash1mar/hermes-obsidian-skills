# checkpoint-1-validate · v2

- Role / sole objective: validate the knowledge-build boundary and record a verifiable decision.
- Allowed inputs: pinned batch, Pass coverage, resource/global reductions, QA and validation reports.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, then `worker-complete` to run batch validation and persist the report and decision. `worker-fail` records a typed failure.
- Fixed IDs / hashes: workflow ID, batch ID, reduction IDs and node fingerprint; report current revisions.
- Bounds: one checkpoint-1 validation, no edits to knowledge outputs.
- Heartbeat: Kanban heartbeat while validation runs.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_1","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when passing validation is durable and the workflow service has rechecked and recorded the decision. A failed validation blocks this card with a report.
- Prohibited: constructing an approval digest, editing decision evidence, Build Finalize or bypassing QA. Manual-mode approval is separate.
