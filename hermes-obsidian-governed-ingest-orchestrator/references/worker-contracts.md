# Fixed worker contracts (v1)

Every `workers/*.md` file is a versioned, immutable-at-run-time worker instruction. The start script snapshots exact bytes into the Vault and binds their SHA-256 hashes to the workflow. The dispatcher sends snapshot content, not the current Skill file. All IDs, hashes, revisions, bounds and allowed paths in a card are binding; a worker stops on missing/stale input.

Each worker receives exactly one Kanban node and returns one JSON outcome. Only the designated deterministic command may persist state. Before doing work, re-read the Vault workflow and verify the node identity, pinned template hash, inputs and parent results. Maintain Kanban heartbeat while running; for Pass slices also maintain the Vault lease heartbeat. Stop at budget/lease/approval boundaries and report a typed failure rather than expanding scope.

Node ordering: source preparation → exact plan → Pass slices → per-resource Reduce → global Reduce → checkpoint 1 validation → human gate → Build Finalize → Vault Finalize plan → checkpoint 2 validation → human gate → release apply → optional Provider sync → acceptance. Gates have no worker template and are never auto-approved. Provider indexes are replaceable; release and workflow records are not.

The `pass-slice` contract is the narrowest: only its named slice and reading packages. It cannot plan, prepare, reduce, finalize or scan the entire batch. Slice success requires durable Pass results and lease completion, not prose.
