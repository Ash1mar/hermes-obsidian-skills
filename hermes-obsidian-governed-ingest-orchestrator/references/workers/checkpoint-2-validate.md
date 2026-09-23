# checkpoint-2-validate · v2

- Role / sole objective: validate the pinned release plan and Vault lint result, then record a verifiable decision.
- Allowed inputs: pinned release plan/digest, completed builds, Vault lint and impact report.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, then `worker-complete` to run release-plan and Vault lint verification and persist the report and decision. `worker-fail` records a typed failure.
- Fixed IDs / hashes: workflow, plan ID/digest, build IDs, node fingerprint and current Vault revision.
- Bounds: one checkpoint-2 packet, no release mutation.
- Heartbeat: Kanban heartbeat during validation.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_2","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when passing validation is durable and the workflow service has rechecked and recorded the decision. A failed validation blocks this card with a report.
- Prohibited: constructing an approval digest, editing decision evidence, apply, Provider sync or ignoring lint failures. Manual-mode approval is separate.
