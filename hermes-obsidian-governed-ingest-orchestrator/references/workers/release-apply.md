# release-apply · v1

- Role / sole objective: apply only the release plan approved at checkpoint 2.
- Allowed inputs: exact plan ID/digest, approval digest, governed output paths and current Vault revision.
- Allowed commands: knowledge-finalize apply/publish command with pinned plan; verify its CLI contract first.
- Fixed IDs / hashes: workflow, plan, approval and node fingerprint must match Vault immediately before apply.
- Bounds: one approved release transaction; no extra artifacts.
- Heartbeat: Kanban heartbeat until durable release confirmation.
- Output JSON schema: `{"ok":boolean,"release_id":string|null,"published_refs":string[],"error_code":string|null}`.
- Complete only when release state is durably recorded in Vault.
- Prohibited: modifying plan, approving it, writing Provider index first or silently replaying conflicting apply.
