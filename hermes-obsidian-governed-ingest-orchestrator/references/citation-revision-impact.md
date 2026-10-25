# Consumed citation amendment: inspection boundary

`recover-reduce-execution` accepts `recovery_scope=reduce-citation-impact/v1`
**only with dry_run enabled**. The workflow must be held or cancelled, with no
native workers, runnable cards or live dispatcher consumers. It never cancels,
resumes, withdraws acceptance, grants a call or writes a domain result.

Use the normal workflow/actor/revision/input_digest fields and a stable recovery
ID. Pin `target_contract_sha256` from `citation_revision_preview.contract_hash()`.
`selections` contains unique `task_id`, the current `pass_id`, and nonempty
`candidate_ids`. These are canonical repository IDs, never cN or pN aliases.
The response contains an immutable-comparable impact digest and SHA256 pins of
current source, task, reading, Pass, source-reduction, global and draft-run records.
Persist the response outside the Vault as audit evidence if needed.

The report distinguishes the selected facts from every candidate whose Pass
identity will change. It traces active source proposals, formal draft decisions
and admitted deferrals. Page membership disjoint from selected facts alone does
not establish unaffectedness: shared inspection QA may change, and worker inputs
also depend on full reading packages, directories and preceding planning state.
Every returned page therefore has `reuse_approved=false`. A full-input proof is
still required before consuming an old native draft under new upstream evidence.

`citation_revision_impact.successor_delta` can check a *separately authenticated
native* successor. It preserves the original task/source window, requires the
immediate predecessor, retains candidate membership, and rejects edits to
unselected candidates. Changed shared inspections/limitations affect the whole
Pass; even unchanged facts acquire a new Pass identity. This comparison does not
authenticate native provenance or create a successor.

The original consumption guard remains active. The separate supported
[bounded local revision chain](citation-local-revision.md) supplies a strict
exception only after durable withdrawal and dependency retirement. Its connected
execution path performs all of the following together:

1. Explicitly withdraw affected acceptance before changing any upstream record.
2. Authenticate the native successor and its bounded delta with cumulative call
   accounting, preserving failures and unknown executions.
3. Atomically retire actual dependent source/global/run pointers and fence old
   executors, with replayable crash recovery and all original audit bytes retained.
4. Reconstruct affected grouping under the native contract. Reuse other drafts
   only after proving current complete input compatibility; never use the impact
   report as that proof or silently grant every changed journal a fresh budget.
5. Reaccept the exact allowed subset and retained gaps, then pause at checkpoint_1.

The existing limited-scope reopen by itself does not perform steps 2–4. Use the
bounded revision contract, not inspection or reopen alone, for consumed recovery.
