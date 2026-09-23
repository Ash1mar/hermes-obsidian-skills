---
name: hermes-obsidian-governed-ingest-orchestrator
description: Start, resume, inspect, approve or cancel a persistent Vault-authoritative governed ingest workflow through deterministic scripts and pinned worker contracts.
---

# Governed ingest orchestration

Use this Skill for one-request, recoverable ingest. For request fields and revisions read `references/workflow-contract.md`; for operator actions read `references/operations.md`; for node/worker boundaries read `references/worker-contracts.md`. When a user brings an old three- or six-stage prompt, read `references/compatibility-profiles.md`. Do not copy a six-part prompt into one conversation or use an interactive session as a background worker.

Call `python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" start --request "<json-file>"` once. The script creates the Vault workflow, pins all twelve versioned templates by SHA-256 and attempts Kanban projection. On retry, use the same request and workflow ID. `dispatcher_unavailable` means the workflow exists but background dispatch is false; report that honestly and resume when Hermes Gateway is available.

For status use `python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" status --workflow-id "<id>" --compact`. For resume/cancel use dispatch script commands; for `approve` use the manage script with an explicit checkpoint approval request. Both checkpoint 1 and checkpoint 2 require human approval recorded in the Vault; no worker may self-approve. Use current revision and a complete mutation digest for every state change. Never edit workflow JSON or pinned snapshots by hand.

`config/orchestration.json` enables bounded pre-batch workers: one `source-prepare` card per scoped Vault source, then `exact-plan` only after every source has a durable ready or failed outcome and at least one is ready. A failed extraction closes only that source card and remains an explicit coverage gap. Exact planning must exclude failed sources and cover the ready UnitSets; final acceptance is `partial` while source gaps remain. Pass still requires an explicitly armed, immutable allowlist of at most eight slice IDs. Reduce, Finalize and checkpoint nodes remain blocked. The Vault ledger remains authoritative; Kanban is rebuildable.
