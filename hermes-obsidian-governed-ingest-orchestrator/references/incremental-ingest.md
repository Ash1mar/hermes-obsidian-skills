# File-ready incremental ingest

Select `workflow_mode=incremental_v1` explicitly. The fixed source list, actor, cumulative `incremental_budget` (calls/input/output tokens and mapping/coordination/writing phase call limits), `incremental_policy.max_maps_per_round`, provider configuration and `incremental_pause_after` must be supplied. The frontend prepares these fields; historical operations keep their original mode.

Use `python3 "<skill-dir>/scripts/manage_incremental_ingest.py" --vault "<vault>" start --request "<request.json>" --record "<record.json>" --bootstrap-skill "<bootstrap-skill>"`. Inspect with `status --run-id "<run>"`. The Vault owns immutable snapshots, seals, maps, round inputs, native evidence, page changes, delivery receipts and closure evidence under `_system/ledgers/incremental-ingest` and `_system/reports/incremental-ingest`.

`control --request` accepts an identity-bound pause/continue/cancel request with run_id, actor, operation_id and expected_revision (control revision). Continue also requires the exact boundary_fingerprint. Supported boundaries: prepare, mapping, coordination, writing, formal-build, release, acceptance, closure. Pause drains admitted tasks and denies new claims. Reconcile never releases a pause.

New ordinary round coordination and writing workspaces also record a `network_recovery` boundary after
three consecutive model route connection or first-event failures. The native dispatcher and HTTP
admission stop new sends, and in-flight sends drain under the existing retry and timeout policy.
The original round remains active in the ledger. The pinned stage report distinguishes acknowledged
results, requests proven not sent and requests whose completion is unknown. Unknown sends retain
their budget reservations and are isolated from recovery dispatch.

Use **继续本轮** after reviewing the network pause. The equivalent `control --request` is an exact
`action=continue` with the normal control revision and `boundary_fingerprint`, plus
`network_recovery={"round_id":"<round>","run_revision":<revision>}`. The authorization is replayable under
the same operation ID. The controller first admits one original unsent request on the affected route.
A successful response releases normal dispatch. A new transport failure parks the round again and
retains its original evidence. A round with no eligible unsent request can process its saved evidence
without a probe or additional model call. Already settled rounds and closure accounting keep their
original contract.

`reconcile --request` uses the saved run_id, actor, operation_id and expected_revision (run revision) to attach a new controller to the same execution. Do not create a new run to hide unknown model consumption. `preview-reuse --request` is read-only and checks current snapshot identities/hashes/contracts. Existing raw refs may only be reused by explicit `incremental_reuse_originals` bindings.

At a drained `closure` checkpoint, `recover-closure --request` takes the run ID, actor, operation ID,
expected control revision, run revision and exact boundary fingerprint. Review `--preview` before
applying with `expected_report_sha256`. The operation verifies each original native attempt and
settles measured usage. A local rejection is zero usage only with an ended zero-use session and no
matching HTTP journal entry. Unconfirmed consumption retains the original reservation and pinned
evidence, and produces a partial outcome. The original release and Provider generation are
revalidated, and guarded `00_目录.md` files are derived from eligible pages at closure.

Reserved usage automatically holds a fully drained run at the closure checkpoint even when it was
not set in the initial pause list. In the console use **核对用量并结案** to view each disposition and
its evidence, then explicitly confirm that report. For a drained legacy interruption, an operator
preview with the current revisions and a null boundary fingerprint establishes that checkpoint and
returns the exact bound request for confirmation. Plain continue cannot release a closure with
outstanding reservations. A previous rejected ordinary close remains in the audit trail.

`recover-delivery --request` requires run_id, actor, expected_revision, round_id, operation_id, disposition (`continue`, `abort_uncommitted`, or `isolate_committed`) and optional stop_after (`build_committed` or `index_verified`). Recovery uses the original Build authorization and draft targets. Never regenerate semantic outputs to clear a delivery diagnostic. Canonical mutation requires a prepared delivery window; abort is allowed only before canonical writes, while isolation must have been preauthorized.

The console's file-node recovery operations reattach the same run after their scoped authorization or acknowledged recovery. A delivery-node request binds the current control revision and reattaches only after the native delivery receipt is verified and the controller lock is released. The controller reuses saved results and proceeds to the next eligible stage. Paused, cancelled and completed runs receive no new dispatch. The applicable durable checkpoint stays in force, including when recovery itself reaches it. Partial closure finishes the original run.

Scoped publication records held plans without a verified page path under `held_plan_gaps`. Each record identifies the frozen change, page identity, reason and pinned Build evidence. A registered path can be recovered from the exact committed page revision. Page exclusions continue to require paths. A required link to a held identity blocks its dependent pages even when that identity has no file.

Native workers perform source preparation, Map, coordination, page writing and formal Build. The controller binds exact snapshots and retains full old page evidence. Hold missing required evidence and direct dependents. Query is unavailable during delivery and remains separately authorized after index readiness. Do not reset failed sources, overwrite parent versions, fabricate native completion or cross durable pauses automatically.

New coordination requests label navigation, source text and registered assets. The writing admission record
pins the original page plan, the exact source-body selection and required asset links. An unambiguous
numbered entry can locate the corresponding registered clause evidence in its sealed Map. Ambiguous or
unavailable material holds the affected page. Writing tries one complete request first, then measures
section or dependency-bound fragments with the final model request and image limits. Every new support
has one writing owner. An indivisible fragment can combine individually bounded original reads
when its complete model request still fits. Build rechecks the source, scope and native receipt for every fragment, and
assembles a single page revision. Updates append the qualified new material while preserving the full
parent body and its citation bindings.

New coordination groups can reconsider a hold that explicitly defers normal writing-time source
inspection. The controller checks available original material and required dependencies against the
sealed Map. Only a group with one verified returned attempt can receive one bounded local model
correction. A structural correction already uses that allowance. The follow-up must cover the
original held evidence for each target and preserve previously accepted identities. True evidence
gaps, unavailable budget and unknown completion remain held. Retry an uncommitted coordination
budget-lock or revision race once under the same live native grant before model invocation.
Original receipts and per-target outcomes remain auditable. Previously pinned coordination groups
retain their original planning revision and decisions.

Structural headings stay in navigation. When a page selects a registered numbered heading
without substantive text, use only mapped original text from that exact source snapshot and
section or hold the page with the missing scope. A heading that contains a substantive numbered
requirement can itself be evidence. New fragment requests require an individual bracketed handle
for each inline citation, including a table handle where its data is used. An unresolved entry
identifies the assigned statement, the exact missing evidence and the conclusion it prevents.
Omitted background blocks writing only when a specific required dependency is missing.
Saved fragment plans and native receipts use their pinned writing instructions.

For an explicitly authorized writing repair, use the same `control --request` command with
`action=continue`, the exact drained writing boundary, and `repair_writing` containing the held
`change_id` values to revise. This applies once to the current frozen round before formal Build.
The original writing checkpoint and receipts stay available. Accepted work is retained, new calls
count against the existing budget, and the revised disposition reaches a new writing pause before
Build can be authorized.

For a manually stopped, already-authorized writing revision, use
`python3 "<skill-dir>/scripts/manage_incremental_ingest.py" --vault "<vault>" recover-writing --request "<request.json>"`.
Preview with `--preview` and the exact run_id, round_id, actor, operation_id, expected_revision
(control revision), run_revision and boundary_fingerprint. Apply with the preview's
`manifest_sha256` as `expected_manifest_sha256`. The new workspace binds the original stop,
page plans, native results, all original attempts and the frozen Maps. It reuses only authenticated
matching evidence assignments. The current writing contract dispatches previously unattempted
independent assignments once within the existing budget. Ended attempts without acknowledgement
retain their evidence and reservations. The resulting disposition reaches the next writing pause.
Ordinary continue requires a recovery entry for an interrupted, stopped writing revision.

At a durable writing checkpoint, use the same recovery command with an explicit
`revise_fragments` list of request IDs from that checkpoint. An empty list previews or
records writing and section eligibility without model calls. A repair needs an authenticated
held receipt and exactly one prior reservation. Mechanical review can preserve a uniquely
bound citation and existing inspection records. Missing inspection, coverage or supplied
evidence can instead require one targeted native result. Unconfirmed calls remain isolated.
Numbered clause continuations are resolved against the same registered snapshot and section,
with original text and asset dependencies measured in the new request. Explicit commentary
anchors can resolve a missing numbered requirement to the unique original clause in the same
snapshot. Oversized replacements can partition only independently writable assignments; a
complete dependency group that still exceeds the limit remains held. The repair binds
the prior receipt, exact duties, evidence, reason and remaining budget.

At the next checkpoint, report accepted original pages and independent section candidates
separately. A candidate requires the complete registered section within the planned evidence,
every writing fragment, all applicable dependency edges and verifiable native receipts.
Candidate identity and provenance are revalidated at normal, separately authorized Build.

Source Map, coordination and formal delivery share the native request replay and stop checks.
Exact acknowledged results are verified when reused. New semantic attempts with unknown prior
completion require a bound stage-specific recovery. The delivery window and source preparation
use their own durable stage contracts.

For operator-controlled file starts, explicitly set `incremental_manual_sources=true` at creation. The fixed source manifest is unchanged; all sources initially wait for admission. Use the frontend's per-source start action or `control --request` with `action=release-source`, exact `source_path`/`source_sha256`, actor, run ID, operation ID and current control revision. Admission while paused does not continue the workflow. Starting another file is a separate manual action; this option adds no delay, timer or default schedule. Already admitted work and its valid results retain their identities.

Use `python3 "<skill-dir>/scripts/manage_incremental_ingest.py" --vault "<vault>" retry-source --request "<request.json>"` for a file node. The request binds run_id, actor, operation_id, control_revision, run_revision, source_path, source_sha256 and retry_kind. `mapping-unconsumed` verifies the sealed source, original job/body hashes, ended native tasks, zero attempts/audits/reservations and bounded retry history. `preparation-unfinished` verifies an admitted file, the original bytes and canonical native preparation binding, and a stopped unfinished task with no durable source outcome. The single authorised fresh conversion uses a separate attempt directory and retains the prior diagnostics. Typed document failures and already prepared sources are ineligible. Both operations preserve the rest of the run and its pauses.

`recover-coordination --request` reattaches the current admitted frozen round. The request binds run_id, round_id, actor, operation_id, expected_revision and control_revision. It reuses the same Maps and native workspace, and only releases a stopped original task when its job, receipts, runtime records and cumulative reservation ledger prove no attempt. Grouped coordination still applies its pinned planning revision. A manual stop is a separate durable boundary.

The console files node actions and writing previews under their own operation IDs. The file node supplies a starting point for reviewing a shared round. The recovery operation remains bound to the round, its original writing or Build authorization and the applicable checkpoint. Terminal runs expose the affected historical scope and evidence for review.

An incremental source qualifies before its first Map request. Its dispatch and workflow locks share a five-second wait budget per attempt, including preview and application. On contention the controller records the lock and wait on the original snapshot, releases its execution slot and schedules at most two retries after two and four seconds. A paused run admits no new attempts. An exhausted wait holds only that source's mapping and is included as a gap in partial closure. Pending and exhausted waits produce no Map completion checkpoint.
