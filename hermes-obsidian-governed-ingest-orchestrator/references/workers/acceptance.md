# acceptance · v7

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. Read the canonical JSON at the card's `worker_binding` path and copy it to one new workspace request file. Keep its workflow/node/task/hash identity unchanged. Call worker-begin before any worker-check. For a Pass slice the binding already provides worker_id and the slice template_hash; add only the lease revision returned by begin. Never copy worker_template_hash or reconstruct identity from environment variables.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

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
