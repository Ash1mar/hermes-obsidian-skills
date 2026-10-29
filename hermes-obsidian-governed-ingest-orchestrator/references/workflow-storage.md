# Fresh workflow roots and protected historical segments

The supported storage operation has two modes: `compact` changes only the mutable
workflow's whitespace; `segments` replaces superseded Pass revision history with
ordered hash-bound references. Both require a stopped workflow, actor/revision,
current raw root SHA-256, original logical fingerprint and stable operation ID.
They never resume, approve or replan work.

Run `python3 "<skill-dir>/scripts/manage_ingest_workflow.py" --vault "<vault>"
migrate-storage --request "<request.json>"`. The request fields are `workflow_id`,
`actor`, `expected_revision`, `operation_id`, `mode`, `expected_root_sha256`,
`logical_fingerprint`, and the ordinary `input_digest`. Native consumers and
leases must have drained; the operation checks quiescence inside the workflow
lock. Deployment and migration are separate operations; neither releases a pause.

## One authority and unchanged logical identities

The original `<workflow-id>.json` path is the only authoritative root. Its private
`_workflow_storage` envelope uses `hermes-workflow-storage/v1` with ordered
`history_prefix` references. The ordinary `pass_revision_history` stores any
later inline tail. Segments live in that workflow's
`storage/pass-history/<operation-id>/`, with ordinal and raw hash in each name.
Duplicates, order and historical `awaiting_review` states are preserved.
Archiving a superseded record never approves it. Active `pass_revision` stays in
the fresh root.

Readers reconstruct the original logical workflow before schema validation,
projection or normalization. Storage fields never enter logical identities.
Only the mutable root's raw bytes change; historical journals, reports, Passes,
snapshots and model requests are not reformatted. There is no writable full-file
mirror. Legacy raw consumers must use the logical API; validating a physical
envelope against the old logical schema fails closed.

## Maintained reader/writer inventory

| Callers | Shared boundary |
| --- | --- |
| status, pause/cancel/continue, repair, revision, skip, plan writes | workflow `_load/_write`, logical schema validation, prefix-preserving compact writer |
| worker authority, live controls, graph identity | service reader including verified snapshots; fresh root at each original check |
| history queries and native recovery | `status_fields` and `VerifiedJsonReads`; complete current validation precedes projection |
| `workflow_guard` and `native_ingest` enumeration | generic `_load_json`, now aware of workflow roots |
| captured pure inputs | `record_snapshot.snapshot_json`, decoding before detached return |
| recovery, policy adoption, migration, reconcile | logical `_write`; `_write_atomic` rejects direct overwrite of segmented roots |
| manage/dispatch/native/watch CLIs | service APIs or generic repository reader; explicit `migrate-storage` CLI |
| isolated native acceptance | decoded logical source becomes an inline isolated copy; original physical bytes still checked for unintended writes |

Search canonical sources and maintained ingest/orchestrator adapters when adding
an entry point. Other raw reads in workflow code address templates, locks,
decisions, reports or domain files, not a durable mutable-root identity.
`expected_root_sha256` is a migration compare-and-swap condition, not a new task
identity. Temporary operators also need the logical reader; old observer scripts
using `json.load` directly must be adapted before reuse.

## Protection and per-read observations

Migration fsyncs and verifies new segments, then sets Linux immutable flags on
the files and their generation directory before publishing the root. Live
snapshot consumers drop all capabilities; privileged maintenance must stop them
first. The protector includes segments as auxiliary evidence, excluding them
from the graph digest. Revocation retains their original immutable flags.

Every root read validates each referenced segment. Without a protected active
generation it rereads and hashes all segments. With one it executes current
registry/protector/lease/metadata checks before reusing bytes. A parsed-root cache
hit cannot skip these guards. Final locked verification bypasses sealed reuse.
Public mutable results stay detached from internal cache data.

The writer preserves the exact archived prefix and rejects its modification or
removal. Later revisions may append an inline tail. It never automatically edits
or unseals an old segment during domain work. A future correction needs a
separate stopped publication operation and corresponding authorization.

## Atomic publication and crash recovery

Under the workflow lock: verify identities and quiescence; write and protect
complete segments; reconstruct and validate the candidate; fsync a prepared
operation containing candidate root and hashes; compare original bytes again;
atomically replace and fsync the root. Logical revision stays unchanged because
logical values did not change. Ordinary mutations keep their revision behavior.

A pre-publication crash leaves the original root authoritative. The same request
can complete preparation/publication. A post-publication crash is recognized by
the candidate hash. Conflicting, missing or changed material fails closed. No
process reuses another process's parsed cache or trusts a stale active marker.

For code recovery to a revision predating this storage contract, first use the
same stopped, identity-bound migration API with `mode=inline`. It materializes
the unchanged logical workflow into the sole root and retains segment audit
data. Verify its logical fingerprint and receipt before restoring code from the
recorded remote Git SHA. Never install an old reader over an active segmented
root or retain old-code payloads as a backup. Inline recovery does not resume.

## Verification

After deployment, run focused storage, verified workflow read, snapshot, recovery
and request checks. Isolate cancellation without revision, same-mtime rewrites,
duplicate/order behavior, missing/replaced/symlinked segments, revocation, process
exit, caller mutation and interrupted publication tests from the real Vault.

Meter compact and segmented sizes separately, compare equal history collections,
and report root bytes, first segment reads, reuse/guard costs and protector work
separately. Application bytes are not physical disk traffic. Without new model
requests, SDK segments and inter-request gaps are unavailable; do not retry held
material for samples.
