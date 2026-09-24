# vault-finalize-plan · v3

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card / `HERMES_KANBAN_TASK`; after begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: prepare a governed release plan from completed knowledge builds.
- Allowed inputs: pinned workflow/batch, completed run IDs, Vault governance and release configuration.
- Allowed commands: knowledge-finalize plan/validate commands only; inspect exact CLI help before use.
- Fixed IDs / hashes: workflow, run IDs, Vault revision and node fingerprint; record plan digest.
- Bounds: one release plan; no apply or Provider write.
- Heartbeat: Kanban heartbeat while planning; fail on changed build results.
- Output JSON schema: `{"ok":boolean,"release_plan_id":string|null,"plan_digest":string|null,"evidence_refs":string[],"error_code":string|null}`.
- Complete when the release plan and validation evidence are persisted for checkpoint 2.
- Prohibited: approval, release apply, broad Vault rewrites or unreviewed publish.
- Call `dispatch_ingest_workflow.py worker-begin` first. After persisting the draft plan, call `worker-complete` with the returned template hash, `release_id` and `plan_id`. The adapter verifies the exact plan, completed builds and absence of blockers, then binds it to the workflow. On failure call `worker-fail`.
