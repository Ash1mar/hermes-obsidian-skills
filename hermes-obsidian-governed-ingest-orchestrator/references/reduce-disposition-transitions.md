# Bounded disposition transitions

`recover-reduce-execution` with `reduce-disposition/v1` can supersede an earlier
nonexecuting deferral or exhausted correction. It does not release a pause or
accept a coverage gap. Valid native bodies remain reusable.

An output selection that already has a disposition must supply `supersedes`
(`report_ref` and the fingerprint of that exact disposition without its runtime
`authorization_ref`), `basis` (1–8 persisted Vault review references and SHA256
values), and a bounded `clarification` explaining new evidence or a corrected
cause. A changed request ID or the same clarification/evidence does not qualify.
The preceding authority must be consumed or nonexecuting. Current input/schema,
all old attempt prefixes, stopped executors and native transcripts remain checked.

Each logical journal has at most four dispositions and two explicitly granted
recovery calls in total, including historical grants. A transition appends at most
one call, without automatic correction; a deferral appends none. Old unknown
execution remains unknown. Calls are never reset by a new task or recovery ID.
Local plan grants retain their existing one-plan/four-child bounds.

Clarification is review guidance, never new source evidence, a candidate/citation
patch or permission to invent QA approval. Targeted body clarifications apply the
assigned-candidate scope rule in the system instruction while retaining the exact
original reading package. Historical requests retain their original wires.

Consumed citation amendments and limited-scope checkpoint acceptance require
their own dependency and acceptance contracts; this transition does not implement
or authorize either operation. Necessary gaps continue to hold `checkpoint_1`.
