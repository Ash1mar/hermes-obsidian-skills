# Bounded disposition transitions

`recover-reduce-execution` with `reduce-disposition/v1` can supersede an earlier
nonexecuting deferral or exhausted correction. It does not release a pause or
accept a coverage gap. Valid native bodies remain reusable.

An output selection that already has a disposition must supply `supersedes`
(`report_ref` and the fingerprint of that exact disposition without its runtime
`authorization_ref`), `basis` (1–8 persisted Vault review references and SHA256
values), and a bounded `clarification` explaining new evidence or a corrected
cause. A changed request ID or the same clarification/evidence does not qualify.
For another recovery the preceding authority must be consumed or nonexecuting. Current input/schema,
all old attempt prefixes, stopped executors and native transcripts remain checked.

Each logical journal has at most four dispositions and two explicitly granted
recovery calls in total, including historical grants. A transition appends at most
one call, without automatic correction; a deferral appends none. Old unknown
execution remains unknown. Calls are never reset by a new task or recovery ID.
Local plan grants retain their existing one-plan/four-child bounds.

Recovery failure → deferral does not require a new corrected cause. The explicit
reason, predecessor and current attempt prefix are still sealed. Deferral may also
surrender an unused recovery call by recording `forfeited_previous_calls`; the
historical grant remains charged against the cumulative two-call limit. It never
erases a reserved/unknown attempt or refunds authority. A later deferral → recovery
still needs new pinned evidence/cause and available cumulative budget.

Clarification is review guidance, never new source evidence, a candidate/citation
patch or permission to invent QA approval. Targeted body clarifications apply the
assigned-candidate scope rule in the system instruction while retaining the exact
original reading package. Historical requests retain their original wires.

Consumed citation amendments require their own dependency and invalidation
contract; this transition does not implement or authorize them. The separate
`reduce-coverage/v1` admission contract can accept an explicitly approved subset,
with necessary gaps retained as incomplete. It grants no citation revision.

The read-only `reduce-citation-impact/v1` inspection is documented in
[citation-revision-impact.md](citation-revision-impact.md). It is a dependency
baseline, not authority to mutate consumed evidence or to reuse drafts after it
changes. Do not describe these capabilities collectively as complete recovery.
