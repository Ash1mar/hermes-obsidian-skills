# Bounded local citation revision

The unified `recover-reduce-execution` entry accepts
`recovery_scope=reduce-citation-revision/v1`. Inspection alone never grants
execution. This contract joins the existing limited-scope reopen, `revise-passes`,
native fixed workers and limited-scope admission; it does not release a pause.

The initial supported scope is one current citation task, one consumed source
result, selected existing candidate IDs, unchanged candidate membership and
original source authority, before Build. Current material/QA/identity/source
records must still match the accepted baseline. An unprovable change, changed
deduplication membership or oversized complete packet stays held. No broad replay
or automatic expansion of the model budget is available.

## Control sequence

Use the ordinary workflow/actor/revision/input_digest fields, stable recovery ID,
and `target_contract_sha256=citation_amendment_control.contract_hash()`.
Every action is operator-only. Actual execution remains a separate authorized
resume; none of these control actions starts a worker itself.

1. Cancel and drain the unreleased limited checkpoint. `action=prepare` selects
   exactly one `task_id`, current `pass_id`, and `candidate_ids` in `selections`.
   Pin 1–8 `basis` references/SHA256 values and a concrete `reason`. Supply explicit
   `budgets` and `cumulative_limits` for `citation`, `source`, `plan`, `body`.
   Initial citation budget is one; source and plan limits are at most two, body
   at most four. Each individual model request has one attempt, no automatic
   correction. An unused authorization is not an API request.
2. `action=start` first records the recovery state, then invokes the supported
   coverage withdrawal and retires only the selected source pointer. Old records,
   drafts, approvals, pauses and unknown calls remain immutable. It authorizes the
   exact existing citation slice through `revise-passes`; its consumption guard
   remains in force. Resume only after that authorization finishes.
3. Native citation reads the exact original package/registered assets and named
   correction. The submission gate verifies actual native request/response,
   canonical worker/Vault ownership, immediate predecessor, original source,
   unchanged unselected candidates and complete candidate membership. It rejects
   a hand-authored replacement even if structurally valid.
4. After native completion/review hold, `action=accept-successor` authenticates
   the ended native session again and accepts the existing Pass revision. A later
   resume dispatches only missing source/global work. Unchanged source proposals
   are mechanically rebound; actual changed source membership is handled by the
   native source worker. Native local planning freezes unrelated identities,
   paths and membership and rechecks global ownership, coverage and collisions.
5. A page is reused only after comparing its candidate content, support material,
   complete original text/images/QA/limits, prior page content and page identity/
   membership. Only exact proven cN/tN/text/heading aliases are normalized. Reuse
   also applies to unchanged sibling pages inside a revalidated proposal.
   Unsupported proof failures never become an automatic rewrite queue. Actual
   affected bodies use only the explicitly budgeted native calls. Semantic
   refusals remain gaps, with their original failure and call state preserved.
6. Native work stops before formal global submission and persists a new immutable
   manifest. Cancel/drain, obtain approval of that exact new manifest and retained
   gaps, then `action=admit` supplies its fresh `approval_ref`/SHA256. Never overwrite
   an older approval file or reuse its manifest hash. Admission reconstructs the
   native local plan/source composition, checks original authorship and per-page
   reuse certificates, and rejects links to held content. Resume materializes the
   new scoped run without more model calls. The ordinary deterministic validator
   stops at checkpoint_1. `action=finish` seals this operation; Build is separate.

## Interruptions and call accounting

The operation state is persisted through prepared → citation → successor_verified
→ downstream → awaiting_acceptance → complete. Full transaction intents precede
control writes; a pending intent fences dispatch, domain writes and Build. Replay
the same durable action after an interruption. Do not mint a replacement workflow,
task or operation to recover a reservation.

Budgets are stored by original task/logical work, independently of Pass/input/native
task hashes. The original journals and known/unknown call evidence remain a pinned
baseline, not zero usage. Each new request is reserved before dispatch. A missing
final response occupies its ordinal; a new worker does not resend it. A completed
native transcript can repair audit persistence after a crash, with the original
fixed-slot schema and all provenance checks retained. Raw responses and normalized
results are kept separately; reconciliation is not parser approval.

`action=authorize-call` can grant one additional attempt for an exact stopped
failed/unknown reservation. Pin `logical_work`, `supersedes_attempt_sha256`, new
`basis` and `reason`; all previous attempts count toward the original cumulative
limit. Renaming an evidence file, changing an operation ID or changing an input
hash is not a new cause or budget. No successful domain output is implicitly lost.

## Restoring an admitted deferral

After a limited checkpoint that retains a body gap, valid citation/source/planning
results need not run again. Cancel/drain, then use `action=reopen-downstream` with
actual held `target_paths`, new pinned `basis` and bounded `clarification`. Only
held work belonging to the original local lineage is eligible. This withdraws the
current scoped aggregate and fences old executors, preserving upstream results.

If an old body call already consumed its allowance, explicitly authorize its
additional call as above. Authorized review guidance may differ in the new wire;
all original evidence, assigned scope and schema must still be identical. It is
not new source evidence. Resume replays valid upstream responses and regenerates
only the eligible held bodies, within the same budget ledger. A new manifest,
fresh exact approval, validation and checkpoint_1 pause are required again.

An absent proof or unresolved essential dependency remains held. This contract
does not fix OCR, expand source authority, silently omit candidates, make skipped
coverage complete, publish, synchronize a Provider or authorize Build Finalize.

## Native ownership across the worker/operator boundary

Trusted dispatch seals the current canonical card ownership before a claim and
the actual ended run/request/response evidence before downstream replay. Workers
read these pinned seals, check their own current canonical binding, native board,
executor and run identity, and authenticate the unchanged native transcript. They
never call the trusted Kanban CLI, clear isolation markers or acquire coordination
permissions. Final successor acceptance and manifest admission still recheck the
actual native cards and ended runs from the trusted operator.

After a program failure with a complete authenticated citation response,
`action=recover-native-execution` may rebind exactly that stopped citation through
the existing Pass execution recovery machinery. Pin the corrected program cause
in `basis` and `reason`. This is replay-only: no model allowance is added, the old
reservation remains charged, a changed wire/schema stays held, and refusals or
unknown completion are ineligible. Resume is a separate authorized operation.
