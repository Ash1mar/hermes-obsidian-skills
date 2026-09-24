# checkpoint-2-validate · v4

- Worker commands commit Vault results only. A response with `kanban_reconciliation_pending` leaves Kanban completion, failure status and successor dispatch to the trusted workflow reconciler. Do not invoke `hermes kanban` from this worker terminal or remove Hermes isolation markers.

- Dispatcher: use the absolute `dispatcher_script` from the card: `python3 "<dispatcher_script>" --vault "<vault>" <worker-command> --request "<request.json>"`. Never resolve a bare filename or select another Skill's dispatcher. In request JSON copy `workflow_id` and `node` from `worker_request`, and `task_id` from this card; the terminal may not receive `HERMES_KANBAN_TASK`. After begin include the returned `template_hash`.
- Stop immediately on failed worker-check or heartbeat, cancellation, stale binding or template mismatch. Do not import internal services to bypass a failed command, resume/cancel the whole workflow, alter pinned contracts, or clear worker environment variables. Report runtime/contract failures on this card; they are not evidence of damaged source material.

- Role / sole objective: validate the pinned release plan and Vault lint result, then record a verifiable decision.
- Allowed inputs: pinned release plan/digest, completed builds, Vault lint and impact report.
- Allowed commands: `dispatch_ingest_workflow.py worker-begin`, then `worker-complete` to run release-plan and Vault lint verification and persist the report and decision. `worker-fail` records a typed failure.
- Fixed IDs / hashes: workflow, plan ID/digest, build IDs, node fingerprint and current Vault revision.
- Bounds: one checkpoint-2 packet, no release mutation.
- Heartbeat: Kanban heartbeat during validation.
- Output JSON schema: `{"ok":boolean,"checkpoint":"checkpoint_2","evidence_refs":string[],"blocking_codes":string[],"error_code":string|null}`.
- Complete when passing validation is durable and the workflow service has rechecked and recorded the decision. A failed validation blocks this card with a report.
- Prohibited: constructing an approval digest, editing decision evidence, apply, Provider sync or ignoring lint failures. Manual-mode approval is separate.
- Use post-ingest lint for release readiness. Query readiness without a Provider is a warning there; `query-ready` and `strict` profiles still treat it as an error. Any other post-ingest lint error blocks automatic continuation.
