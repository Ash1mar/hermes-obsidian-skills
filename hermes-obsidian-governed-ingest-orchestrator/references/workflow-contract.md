# Workflow contract

The authoritative record is `_system/ledgers/ingest-workflows/<workflow_id>.json` in the Vault. `start` requires `workflow_id` (`ingest-...`), `actor`, `profile` (`compact-3` or `diagnostic-6`), `scope`, `expected_revision: 0`, and `input_digest`. An existing knowledge-build `batch_id` is optional. See the canonical `ingest_workflow` JSON schema in `lib/hermes_source_units/schemas/contracts.json` for scope details.

For mutations, calculate `input_digest` as `sha256:` plus the canonical request fingerprint excluding `input_digest`; the `digest` CLI command can do this. Use the record's current `revision` as `expected_revision`. Do not regenerate a workflow ID on retry. A duplicate start with identical digest returns the existing record; a different digest is a conflict.

`template_pins` contains one entry per worker kind: `kind`, versioned `template_id`, `template_hash`, and Vault-relative snapshot path. Pinning stores the exact UTF-8 template bytes in the workflow's Vault directory. Once pinned, the set cannot be replaced. Dispatch verifies every snapshot hash before creating active cards; missing/changed content fails closed. Changing Skill files affects only new workflows. Kanban task idempotency keys also include the pinned template hash.

The Vault owns source, batch, Pass, Reduce, release, checkpoint and final acceptance state. Kanban tasks are only rebuildable projections. A card reaching `done` does not prove that its domain operation succeeded; reconciliation checks Vault state.

`profile` controls only the read-only `display` object returned by compact status: phase number, total, name and pending human checkpoint. It does not change transition order, worker graph, budgets, idempotency keys, approvals or authoritative stage. The mapping is documented in `compatibility-profiles.md`.

`dispatch_policy` is Vault-authoritative and independent of the host rollout switch. It starts disabled. `canary-preview` chooses at most eight ready slices without writing; `arm-canary` requires the exact IDs, digest and expected workflow revision. The first selection is immutable; disarming stops later worker checks and can only rearm the same IDs. An unarmed workflow or a Pass outside this allowlist cannot receive a worker lease through the adapter, even if the host flag is enabled. Release/Reduce/checkpoint nodes remain blocked throughout the canary.
