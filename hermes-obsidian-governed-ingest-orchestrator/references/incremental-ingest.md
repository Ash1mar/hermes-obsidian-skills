# File-ready incremental ingest

Select `workflow_mode=incremental_v1` explicitly. The fixed source list, actor, cumulative `incremental_budget` (calls/input/output tokens and mapping/coordination/writing phase call limits), `incremental_policy.max_maps_per_round`, provider configuration and `incremental_pause_after` must be supplied. The frontend prepares these fields; historical operations keep their original mode.

Use `python3 "<skill-dir>/scripts/manage_incremental_ingest.py" --vault "<vault>" start --request "<request.json>" --record "<record.json>" --bootstrap-skill "<bootstrap-skill>"`. Inspect with `status --run-id "<run>"`. The Vault owns immutable snapshots, seals, maps, round inputs, native evidence, page changes, delivery receipts and closure evidence under `_system/ledgers/incremental-ingest` and `_system/reports/incremental-ingest`.

`control --request` accepts an identity-bound pause/continue/cancel request with run_id, actor, operation_id and expected_revision (control revision). Continue also requires the exact boundary_fingerprint. Supported boundaries: prepare, mapping, coordination, writing, formal-build, release, acceptance, closure. Pause drains admitted tasks and denies new claims. Reconcile never releases a pause.

`reconcile --request` uses the saved run_id, actor, operation_id and expected_revision (run revision) to attach a new controller to the same execution. Do not create a new run to hide unknown model consumption. `preview-reuse --request` is read-only and checks current snapshot identities/hashes/contracts. Existing raw refs may only be reused by explicit `incremental_reuse_originals` bindings.

`recover-delivery --request` requires run_id, actor, expected_revision, round_id, operation_id, disposition (`continue`, `abort_uncommitted`, or `isolate_committed`) and optional stop_after (`build_committed` or `index_verified`). Recovery uses the original Build authorization and draft targets. Never regenerate semantic outputs to clear a delivery diagnostic. Canonical mutation requires a prepared delivery window; abort is allowed only before canonical writes, while isolation must have been preauthorized.

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

Source Map, coordination and formal delivery share the native request replay and stop checks.
Exact acknowledged results are verified when reused. New semantic attempts with unknown prior
completion require a bound stage-specific recovery. The delivery window and source preparation
use their own durable stage contracts.

For operator-controlled file starts, explicitly set `incremental_manual_sources=true` at creation. The fixed source manifest is unchanged; all sources initially wait for admission. Use the frontend's per-source start action or `control --request` with `action=release-source`, exact `source_path`/`source_sha256`, actor, run ID, operation ID and current control revision. Admission while paused does not continue the workflow. Starting another file is a separate manual action; this option adds no delay, timer or default schedule. Already admitted work and its valid results retain their identities.

For an exact source Map task that stopped before any model consumption, use `python3 "<skill-dir>/scripts/manage_incremental_ingest.py" --vault "<vault>" retry-source --request "<request.json>"`. The request binds run_id, actor, operation_id, control_revision, run_revision, source_path, source_sha256 and retry_kind=`mapping-unconsumed`. The operator entry independently verifies the current sealed source, original job/body hashes, ended native tasks, zero attempts/audits/reservations and bounded retry history. It unblocks only those same native tasks, preserves completed work and never releases a pause. Returned or unknown model consumption requires its existing evidence-based recovery, not this retry. A failed unchanged PDF does not gain a new conversion attempt through this entry.
