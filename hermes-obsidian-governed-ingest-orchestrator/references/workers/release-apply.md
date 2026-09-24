# release-apply · v4

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: apply only the release plan approved at checkpoint 2.
- Allowed inputs: exact plan ID/digest, approval digest, governed output paths and current Vault revision.
- Allowed commands: knowledge-finalize apply/publish command with pinned plan; verify its CLI contract first.
- Fixed IDs / hashes: workflow, plan, approval and node fingerprint must match Vault immediately before apply.
- Bounds: one approved release transaction; no extra artifacts.
- Heartbeat: Kanban heartbeat until durable release confirmation.
- Output JSON schema: `{"ok":boolean,"release_id":string|null,"published_refs":string[],"error_code":string|null}`.
- Complete only when release state is durably recorded in Vault.
- Prohibited: modifying plan, approving it, writing Provider index first or silently replaying conflicting apply.
- Call `dispatch_ingest_workflow.py worker-begin` before applying the bound plan. After the release manifest is durable, call `worker-complete` with the returned template hash. The adapter validates the exact release before advancing. On failure call `worker-fail`.
