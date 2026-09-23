# Fixed worker contracts (v1)

Every `workers/*.md` file is a versioned, immutable-at-run-time worker instruction. The start script snapshots exact bytes into the Vault and binds their SHA-256 hashes to the workflow. The dispatcher sends snapshot content, not the current Skill file. All IDs, hashes, revisions, bounds and allowed paths in a card are binding; a worker stops on missing/stale input.

Each worker receives exactly one Kanban node and returns one JSON outcome. Only the designated deterministic command may persist state. Before doing work, re-read the Vault workflow and verify the node identity, pinned template hash, inputs and parent results. Maintain Kanban heartbeat while running; for Pass slices also maintain the Vault lease heartbeat. Stop at budget/lease/approval boundaries and report a typed failure rather than expanding scope.

Node ordering: source preparation → exact plan → eight-slice canary → remaining Pass slices → per-resource Reduce → global Reduce → checkpoint 1 validation and decision → Build Finalize → Vault Finalize plan → checkpoint 2 validation and decision → release apply → optional Provider sync → acceptance. In `auto_full`, a gate is satisfied only by a hashed passing decision report that the workflow service revalidates; manual mode requires explicit human approval. Gates have no worker template. Provider indexes are replaceable; release and workflow records are not.

For every downstream card call `dispatch_ingest_workflow.py worker-begin` before the named domain command and `worker-complete` after its durable Vault result. Use the returned template hash and current card binding. On a failed domain operation call `worker-fail` with a typed reason; do not claim completion from prose or Kanban state. A completed card triggers the next eligible node through Vault reconciliation.

The `pass-slice` contract is the narrowest: only its named slice and reading packages. It cannot plan, prepare, reduce, finalize or scan the entire batch. Slice success requires durable Pass results and lease completion, not prose.
