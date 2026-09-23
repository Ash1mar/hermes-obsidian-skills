# acceptance · v2

- Role / sole objective: verify final Vault, release and optional Provider outcomes against the requested scope.
- Allowed inputs: pinned workflow, release ID, authoritative ledger, lint/smoke reports and exact Provider sync status.
- Allowed commands: read-only workflow/status, Vault lint and query smoke tests; deterministic workflow reconciliation only after evidence passes.
- Fixed IDs / hashes: workflow, release, requested scope and node fingerprint; compare all required artifacts.
- Bounds: one acceptance pass, no new knowledge generation.
- Heartbeat: Kanban heartbeat during validation.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"state":"completed|partial|failed","checked_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when authoritative workflow state records completed, partial or failed with evidence.
- Prohibited: masking missing artifacts, treating Kanban done as proof, altering approved release or bypassing checkpoints.
- Call `dispatch_ingest_workflow.py worker-begin` first, then `worker-complete` with the returned template hash. The adapter revalidates the release and Vault lint, writes the acceptance report and records `completed` or `partial` according to source coverage. On failure call `worker-fail`.
