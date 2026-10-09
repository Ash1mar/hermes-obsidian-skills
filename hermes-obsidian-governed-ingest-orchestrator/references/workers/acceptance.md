# acceptance

<!-- hermes-program-worker/v1 -->

Execution owner: the trusted reconciler pulls this reserved `ingest-program` card through a native Kanban claim and starts the installed program entrypoint. No conversational worker, terminal command selection, request copying, polling by a model, or CLI fallback.

The program reads the current canonical binding, checks task/input/template identities, executes only this node and submits its durable outcome through the existing worker contract. Native run/PID/heartbeat/failure records remain authoritative for execution; the Vault remains authoritative for domain results.

Cancellation, stale binding, unavailable input, disabled Provider and validation failure stop affected work through the supported failure report. Do not reconstruct identities, retry unchanged bindings, fabricate review evidence or reinterpret runtime errors as damaged source material.

Durable pauses and checkpoint decisions retain their existing contracts. Only the user-authorized trusted operator may continue a pause. Completed valid results are reused after their current checks; semantic Pass, Reduce and page review are separate model work.

Fixed program operation: acceptance.
