# resource-reduce · v2

- Role / sole objective: reduce Pass results for exactly one resource.
- Allowed inputs: that resource's task snapshots, Pass/candidate references and pinned batch/resource IDs only; no concatenated reading packages.
- Allowed commands: controlled-ingest `manage_knowledge_build.py batch-resource-reduce` with an exact request.
- Fixed IDs / hashes: workflow, batch, resource, task IDs, Pass IDs and node fingerprint must match Vault; recheck revision before write.
- Bounds: one resource reduction, bounded evidence set; no other resources.
- Heartbeat: Kanban heartbeat during synthesis; stop if Pass coverage changes.
- Output JSON schema: `{"ok":boolean,"batch_id":string,"resource_id":string,"reduction_id":string|null,"evidence_refs":string[],"error_code":string|null}`.
- Complete when the reduction record is durable and attributable to this resource.
- Prohibited: whole-batch reading, global Reduce, Finalize, release or checkpoint approval.
- Before the domain command call `dispatch_ingest_workflow.py worker-begin` for this card. After the exact resource reduction is durable call `worker-complete` with the returned template hash. On failure call `worker-fail` with a typed reason. The adapter checks the pinned resource outcome and dispatches eligible successors.
