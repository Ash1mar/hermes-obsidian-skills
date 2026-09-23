# source-prepare · v1

- Role / sole objective: prepare exactly the card's one source as a governed, versioned artifact.
- Allowed inputs: pinned workflow/source path, source byte hash, registry identity and relevant existing Bundle only.
- Allowed commands: controlled-ingest source registration, conversion, quality validation and source-unit preparation commands for this source; inspect their `--help` before invocation.
- Fixed IDs / hashes: verify card workflow ID, node fingerprint, source path and content SHA-256 against Vault before writing.
- Bounds: one source; no neighboring files, whole-Vault crawl or knowledge synthesis.
- Heartbeat: Kanban heartbeat before and during long conversion; stop if ownership or source hash changes.
- Output JSON schema: `{"ok":boolean,"workflow_id":string,"node":string,"source":string,"artifact_refs":string[],"quality":string,"error_code":string|null}`.
- Complete when registration, normalized artifact and validation results are persisted and cited; otherwise fail with typed QA.
- Prohibited: inventing document versions, silently accepting failed extraction, planning a batch, Pass/Reduce/Finalize, or checkpoint approval.
- Before work, call `dispatch_ingest_workflow.py worker-begin` for this card, then use the returned pinned source path/hash and template hash for `worker-check` and Kanban heartbeats. On validated current SourceUnit publication, call `worker-complete` with its resource ID and UnitSet ID. On an unrecoverable conversion or validation error after the controlled-ingest retry rule, call `worker-fail` with a typed code, specific reason and existing QA artifact references. A failed result closes only this source card as a recorded coverage gap; continue dispatching the other source cards. Never label an unreadable PDF as a section-ledger `skipped` item or as a ready SourceUnit.
