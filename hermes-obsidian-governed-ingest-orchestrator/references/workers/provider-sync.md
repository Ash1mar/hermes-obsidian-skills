# provider-sync · v3

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card / `HERMES_KANBAN_TASK`; after begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: project the exact published release into the configured retrieval Provider.
- Allowed inputs: pinned release ID, provider config and Vault retrieval metadata; no unpublished drafts.
- Allowed commands: knowledge-finalize exact-release Provider sync/status commands only.
- Fixed IDs / hashes: workflow, release, provider/model/embedding fingerprints and node fingerprint; verify target before writing.
- Bounds: one release projection; Provider indexes remain replaceable, outside Vault by default.
- Heartbeat: Kanban heartbeat during indexing; report unavailable Provider explicitly.
- Output JSON schema: `{"ok":boolean,"release_id":string,"provider":string,"index_fingerprint":string|null,"error_code":string|null}`.
- Complete when sync status for this exact release is durably auditable.
- Prohibited: changing authoritative Vault release, QMD on intranet, indexing a different release or claiming success after skip/failure.
- When the workflow requests Provider sync, call `dispatch_ingest_workflow.py worker-begin` first. After an auditable index manifest for the exact release is ready, call `worker-complete` with the returned template hash. The adapter rechecks release and index fingerprints; on failure call `worker-fail`.
