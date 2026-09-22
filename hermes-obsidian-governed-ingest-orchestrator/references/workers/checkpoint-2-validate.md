# checkpoint-2-validate · v1

- Role / sole objective: present a release-ready validation packet to the second human checkpoint.
- Allowed inputs: pinned release plan/digest, completed builds, Vault lint and impact report.
- Allowed commands: read-only release plan validation and Vault lint; persist only checkpoint evidence reference.
- Fixed IDs / hashes: workflow, plan ID/digest, build IDs, node fingerprint and current Vault revision.
- Bounds: one checkpoint-2 packet, no release mutation.
- Heartbeat: Kanban heartbeat during validation.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_2","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when evidence is durable and the human gate remains blocked pending explicit approval.
- Prohibited: self-approval, apply, Provider sync or ignoring lint failures.
