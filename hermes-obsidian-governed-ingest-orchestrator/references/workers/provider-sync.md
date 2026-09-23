# provider-sync · v2

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
