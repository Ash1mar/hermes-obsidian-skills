# Completed Pass semantic revisions

## Missing original dependency inspections discovered during Reduce

The same revision interface supports `revision_scope: reduce-dependencies/v1` only for
a cancelled, released Reduce before any resource/global reduction or build result.
It requires the existing full dispatch policy, no live native consumers/leases, exact
current resource failure task IDs and SHA-256 values, and latest non-skipped citations
that actually lack inspections of currently required registered dependencies. It does
not reopen unaffected tasks or permit arbitrary semantic correction through this scope.

Use the trusted preparation command, which records mechanical missing-ref/trigger/package
diagnostics and other-task coverage, then preview the requests before applying:

```bash
python3 "<skill-dir>/scripts/prepare_reduce_repairs.py" --vault "<vault>" --workflow-id "<observed-id>" --repair-id "<stable-id>" --output-dir "<operator-dir>"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" revise-passes --dry-run --request "<operator-dir>/revision-request.json"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" revise-passes --request "<operator-dir>/revision-request.json"
```

The request pins unchanged pause history as `evidence_digest`, bound inspection failures,
original package/latest citation identities, diagnostic evidence and a fixed semantic
revision template. Applying remains cancelled and never resumes. Native fixed workers
receive only selected tasks, their predecessor citations and complete current required
reading supplements. They must author a next citation, preserving existing records;
an unchanged-confirmation receipt cannot fill missing inspection. New presentation does
not replace the original package or alter canonical task/slice ownership and budgets.

During revision the graph dispatches selected citation workers only. Completion records
`awaiting_review` and a separate `review_report_ref`/`review_evidence_digest`; released
pause history is never rewritten or released again. `revision_review` is a durable review
hold derived from that revision state, not a new configurable stage. It blocks all Reduce
work and cannot be released by `continue-workflow`. Explicit `accept-pass-revision` must
pin that review digest and reviewer evidence. Acceptance and a later authorized resume
are separate; do not auto-accept a bounded observation or repeat valid Passes.

Other-task inspection cannot stand in for the current citation. Current required refs,
source hashes, native binding and ordinary Pass submission checks remain authoritative.
No evidence truncation, optionalizing required assets, source reset or model fallback is
allowed. A single indivisible task that cannot fit stays blocked with its original evidence.

For a legacy `knowledge-pass/v1` slice whose creation configuration differs from a later
execution amendment, `recover-pass-execution` scope `legacy-slice-config/v1` may authenticate
an operator-supplied historical snapshot against the released exact-plan digest. The request
pins the current `revision_id`, `historical_snapshot_ref` and exact failed node/task/SHA entries.
Both original batch and slice bytes must match exact-plan's file hashes; current ordered task
inputs, ownership and template must reproduce the full original slice fingerprint. Only the
specific `slice input fingerprint changed` refusal is eligible. Recovery remains cancelled,
grants one fresh bound attempt and never changes slices, sources, Passes, pauses or current
batch limits. Workers repeat this authenticated creation check; current evidence, permissions
and execution budgets remain mandatory. Missing/tampered history stays blocked. Other failed
nodes remain isolated; a local legacy-path refusal alone does not justify cancelling unrelated
revision work.

The existing `mechanical/v1` recovery also covers selected unfinished slices of the
current running dependency revision, with its exact `revision_id`. Ordinary source,
failure-hash, binding, no-consumer and one-retry guards remain mandatory. An incomplete
native-model-request transport report is eligible only when the last actual native
attempt ended explicitly as `Vault workflow cancelled`, without a completed result;
its observed revision packet and selected predecessors must still match current input.
Other generic program/permission/semantic errors stay blocked. Recovery does not accept
the revision or release its review hold.

Fixed revision packets use the pinned revision template for typed-submission capability,
including original slices. Their result slots allow only authored revisions or explicit
blocking, never first-citation unchanged confirmation. For a failed attempt admitted through
the old packet contract, `recover-pass-execution` scope `revision-citation-packet/v1` requires
the current revision ID and exact failure binding. It recognizes only the old bounded-slice
capability refusal or reviewed-candidate mismatch, then authenticates the original native
input/packet, current required evidence and selected predecessor citations before granting
one fresh attempt. It also recognizes the exact required-inspection draft refusal that
the older native classifier incorrectly escalated to an execution failure. This path
requires exactly one original citation call with an unused two-call correction budget,
current matching pending input, and preserved correctly bound partial citations. It
continues only that one remaining correction for the rejected slot; already completed
tasks are not replayed. Any new failure exhausts that grant. Arbitrary permission,
stale-input and semantic errors remain ineligible. Packet recovery is limited to once
per recognized corrected cause. A canonical-binding bootstrap KeyError can recover only
with a matching pending packet, an ended native run, no semantic audit and a present
native timeline showing no SDK model attempt; it retains any earlier remaining quota.

## Held Pass boundary revisions

Use this general operator recovery for reviewed semantic/citation errors at the current durable `canary` or `pass` pause, before Reduce consumes the evidence. Preserve the same workflow/batch, source outcomes, plan, immutable Pass files and unselected task results. This interface does not authorize crossing a boundary.

1. Inspect status, current pause report/digest, completed slice and latest citation Pass IDs. Confirm no active leases/native workers. Read reviewer findings and store their evidence in the Vault. Do not modify domain outputs to create review evidence.
2. Build a request containing workflow_id, actor, expected_revision, stable revision_id, current evidence_digest, reason, evidence_refs, tasks (task_id, latest citation pass_id, reason), and worker_template containing `references/pass-revision-worker.md` exactly. Use the complete mutation input_digest. Select affected tasks from review evidence; do not hardcode a source filename, candidate name or Vault identity into repair logic.
3. Execute `python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" revise-passes --request "<request.json>"`. The runtime validates current inputs, pins a separate immutable revision contract and request, and opens only selected completed slices with new native bindings. Unselected completed native identities remain bound when the graph narrows. Request retries reuse the same ID and exact request. `sync`/watch can finish interrupted projection under this recorded authorization. Do not unblock cards manually.
4. Native workers read only selected predecessor citations and their original bounded packages, review every claim/condition/exception, append the authorized next citation sequence after bound preflight and complete the slice. Identity-bound revision_id/supersedes_pass_id fields are supplied by runtime. An unselected task in that slice cannot write. No candidate Pass or historical file is overwritten.
5. The reconciler preserves the original pause report and writes a new report under the revision audit directory. It verifies protected evidence, retains the same boundary and continuation history and records `awaiting_review`. Other slices and downstream work remain held. Original citations remain audit evidence; superseded candidates cannot enter Reduce.
6. Present the new citations and evidence to the reviewer and stop. Only explicit reviewer approval permits `python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>" accept-pass-revision --request "<request.json>"`, containing workflow_id, actor, expected_revision, revision_id, current evidence_digest, persisted reviewer evidence_refs and input_digest. Acceptance is idempotent and leaves the pause held. Any separately authorized `continue-workflow` must still name that current pause and digest.

If a completed revision needs more correction, a new explicitly authorized revision may select its latest citation while `awaiting_review`; retain the previous revision as history. Each slice retains the identity of its latest authorized revision, including when a later revision selects other slices. Do not accept invalid content just to authorize another revision. Stale evidence, changed sources, consumed reduction, foreign tasks or active leases are rejected. Failed revision workers remain blocked under the current identity; preserve diagnostics and use only supported trusted recovery, never mutate leases or re-run source preparation/exact-plan.

Trusted `sync` can restore the native identity of a completed Pass slice from its existing canonical binding. It verifies workflow, actor, node, input and template identities, the original native card and its completed/archived state, and current held-pause evidence. It does not overwrite the binding, regenerate a Pass, or run a worker. A displaced duplicate card is archived; its failure report remains historical evidence. An archived original with valid completed Vault results stays archived and reusable. Invalid canonical/native evidence retains the execution block.
