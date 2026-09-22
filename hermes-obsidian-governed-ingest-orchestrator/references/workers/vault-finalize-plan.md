# vault-finalize-plan · v1

- Role / sole objective: prepare a governed release plan from completed knowledge builds.
- Allowed inputs: pinned workflow/batch, completed run IDs, Vault governance and release configuration.
- Allowed commands: knowledge-finalize plan/validate commands only; inspect exact CLI help before use.
- Fixed IDs / hashes: workflow, run IDs, Vault revision and node fingerprint; record plan digest.
- Bounds: one release plan; no apply or Provider write.
- Heartbeat: Kanban heartbeat while planning; fail on changed build results.
- Output JSON schema: `{"ok":boolean,"release_plan_id":string|null,"plan_digest":string|null,"evidence_refs":string[],"error_code":string|null}`.
- Complete when the release plan and validation evidence are persisted for checkpoint 2.
- Prohibited: approval, release apply, broad Vault rewrites or unreviewed publish.
