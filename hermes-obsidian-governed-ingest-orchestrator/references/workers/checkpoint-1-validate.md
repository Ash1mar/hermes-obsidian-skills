# checkpoint-1-validate · v1

- Role / sole objective: prepare an evidence-backed human review for the knowledge-build boundary.
- Allowed inputs: pinned batch, Pass coverage, resource/global reductions, QA and validation reports.
- Allowed commands: read-only batch status and validators; persist only a checkpoint evidence reference through the workflow service.
- Fixed IDs / hashes: workflow ID, batch ID, reduction IDs and node fingerprint; report current revisions.
- Bounds: one checkpoint-1 validation, no edits to knowledge outputs.
- Heartbeat: Kanban heartbeat while validation runs.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_1","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when review evidence is durable and the human gate remains blocked pending explicit approval.
- Prohibited: self-approval, synthetic approval digest, Build Finalize or bypassing QA.
