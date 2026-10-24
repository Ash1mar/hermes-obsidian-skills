# Limited Reduce acceptance

`recover-reduce-execution` supports `recovery_scope=reduce-coverage/v1`.
This is an explicit change to acceptance scope. Ordinary defer/resume retains
the full-coverage gate. Only the owner operator can admit exact persisted,
hash-pinned scope and approval documents for the same stopped workflow.

The manifest lists every allowed native page (identity, candidates, destination,
native and submitted content hashes), every necessary deferred candidate and
its reason. Admission authenticates the complete proposal plan and each allowed
native body against its finite journal/transcript, rechecks current source
inputs, and rejects overlaps or explicit links to held pages. No semantic call
is granted. A missing proof fails closed; a changed task ID grants no budget.

The global result and build run carry the immutable scope reference. Necessary
gaps are recorded in omission accounting and remain incomplete. Validation may
accept the approved page subset at checkpoint_1, with `outcome=limited_scope`
and `coverage_complete=false`. Downstream validation/finalization permits only
the exact page, claim and content set; the workflow can terminate only as partial.
Independent page review remains a Build Finalize activity. Reaching this pause
does not authorize continuing it, publishing, or Provider synchronization.

Before Build consumption, the same recovery entry point accepts
`recovery_scope=reduce-coverage-reopen/v1`. It requires the exact active scope,
an unreleased checkpoint_1, cancellation/drain, draft-only runs, and pinned new
evidence or corrected cause. It retires global/run control pointers while
retaining their immutable artifacts, native drafts, Passes, failure/unknown
records and cumulative budgets. It resets the checkpoint to pending and Reduce
to cancelled; it does not append a continuation or grant calls. Restoration
then uses existing supported evidence/semantic recovery permissions. Retired
scoped runs cannot be finalized after withdrawal.

The two control-record writes use a persistent pending journal. Dispatch/resume
is blocked until the exact request is replayed to completion. Previous pause
and checkpoint decision bytes are retained in the recovery report before the
canonical pause is retired, permitting a fresh evidence-bound pause later.
Reopening after checkpoint continuation, Build completion or publication is
outside this operation's authority.
