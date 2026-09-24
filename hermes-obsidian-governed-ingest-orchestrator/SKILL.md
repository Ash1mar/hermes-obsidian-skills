---
name: hermes-obsidian-governed-ingest-orchestrator
description: Start, resume and inspect a persistent Vault-authoritative governed ingest workflow with pinned workers, bounded canary and verified automatic checkpoints.
---

# Governed ingest orchestration

Use this Skill for one-request, recoverable ingest. For request fields and revisions read `references/workflow-contract.md`; for operator actions read `references/operations.md`; for node/worker boundaries read `references/worker-contracts.md`. When a user brings an old three- or six-stage prompt, read `references/compatibility-profiles.md`. Do not copy a six-part prompt into one conversation or use an interactive session as a background worker.

Call `python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" start --request "<json-file>"` once. The script creates the Vault workflow, pins all twelve versioned templates by SHA-256 and attempts Kanban projection. On retry, use the same request and workflow ID. `dispatcher_unavailable` means the workflow exists but background dispatch is false; report that honestly and resume when Hermes Gateway is available.

For status use `python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" status --workflow-id "<id>" --compact`. For resume/cancel use dispatch script commands. In `scope.execution_mode: auto_full`, the dispatcher arms exactly eight ready Pass slices, verifies all eight, promotes remaining slices, and dispatches the downstream graph. Each checkpoint writes a validation report and a hashed decision record. The workflow service independently rechecks the evidence before recording `hermes:auto` approval and continuing. A failed check stops the affected card and reports the blocking codes. In manual mode an explicit human approval remains required. Use current revision and a complete mutation digest for every state change. Never edit workflow JSON or pinned snapshots by hand.

`config/orchestration.json` enables one `source-prepare` card per scoped Vault source, then `exact-plan` only after every source has a durable ready or failed outcome and at least one is ready. A failed extraction closes only that source card and remains an explicit coverage gap. Exact planning excludes failed sources and covers ready UnitSets; final acceptance is `partial` while source gaps remain. In `auto_full` the dispatcher proceeds through Pass, Reduce, both checkpoints, Build Finalize, release planning and apply, optional Provider sync, and acceptance. Each worker calls `worker-begin` before work and `worker-complete` after durable Vault results; the adapter validates parent outcomes and pinned hashes. The Vault ledger remains authoritative; Kanban is rebuildable.

For a trial that must stop after eight slices, set `scope.execution_mode: canary_only`. Source preparation and exact planning still run for the full requested source scope; only the eight selected Pass slices execute. Completion writes a canary report and does not promote remaining slices or dispatch Reduce. `auto_full` explicitly authorizes continuation beyond the canary.

Worker commands use the absolute dispatcher path embedded in each card. The controlled-ingest worker CLI is a compatibility alias to this configured orchestrator. A source worker first calls `worker-register-source` to reuse or allocate technical IDs without asserting document authority. Whole-workflow source coverage is progress information, not the card's assigned source.

For a cancelled pre-batch workflow with a proven erroneous failed-source outcome or obsolete pinned contracts, use the operator-only `repair-preparation` operation in `references/operations.md`. Never edit the ledger manually or reuse a failed workflow by changing IDs. Repair preserves a before snapshot, installs a new immutable template pack, invalidates old card bindings and leaves the workflow cancelled until explicit resume.
