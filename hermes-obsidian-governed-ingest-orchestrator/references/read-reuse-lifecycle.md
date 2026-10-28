# Recovery evidence snapshot lifecycle decision

Status: evaluated for the current staged Build batch; cross-authentication reuse
is not enabled. This is the third batch's feasibility/contract result, not a
storage migration or a claim that cumulative dependency GB has been removed.

## Observed authority and writers

`fixed_semantic_recovery.native_reply` checks worker authority on entry where
required and always on exit. `semantic_output.execute` supplies its adjacent
entry check and authenticates each saved response separately. The existing
`authenticated_replay` interface is intentionally not wrapped around the whole
recovery loop. Its two checks do not preserve the old per-response observation
positions by themselves.

Workflow controls, historical pointers and Pass revision records currently live
in the full workflow JSON. `source_units._write_atomic` replaces ordinary files;
`workflow_guard.workflow_write_guard` and `_exclusive_lock` constrain cooperating
writers. They do not make Vault bytes immutable against other programs. Batch,
task, slice and Pass writers use ordinary filesystem records. Direct same-mtime
replacement and symlink replacement remain possible independently of workflow
revision. A pointer's content hash binds interpretation to bytes that are read;
it does not prove that the current file still has those bytes without reading it.

Therefore an adjacent captured report's hash and parse may share its bytes, but
proof of one historical response cannot authorize omitting the next response's
dependency scan. Every new gate still reads workflow controls and validates
current dependency membership and bytes. Parse, shape and canonical derivations
may reuse only a currently matched byte/schema/rule version. Every captured
calculation ends at its lexical return; failed hashes publish no trusted view.
No capture can survive a model call, callback, wait, permission boundary or restart
as a current authority proof. Detached results never grant another attempt.

## Protocol required before enabling broader reuse

| Data | Required writer/storage guarantee | Failure/recovery rule |
| --- | --- | --- |
| Dynamic controls | A small authoritative control record updated by every pause, cancel, binding, template, allowance and state writer | Each gate reads it; missing/inconsistent versions fail closed; no inferred revision equivalence |
| Historical evidence | Enforced immutable content storage or a complete audited change-generation protocol, including all non-Hermes writers | A first full hash verifies bytes; version change invalidates parse, proof and membership snapshots; unauthenticated direct writes remain detectable |
| Dependency membership | A crash-consistent authoritative manifest updated atomically with create/delete/replace and symlink changes | Partial commits, missing members or failed recovery block affected work; rebuild the manifest from full verification |
| Snapshot lifetime | Bound Vault, workflow, dependency set, canonical identity, schema, reader contract and control generation | Expire at any changed binding, epoch, error, callback boundary or restart; never cache permission verdicts |

A new sidecar digest is insufficient while the original file can change without
updating it. Timestamp/size checks and hash-like filenames are insufficient too.
Splitting the control record requires an audit of all workflow mutation methods,
native bridge/program-worker entry points, deployment tools and external writer
access; a durable journal and reconciliation protocol must define the authority
during crashes. Neither that contract nor enforced immutable storage exists in
the current layout. Migrating partial historical/task records for one performance
window would expand scope and would not meet the current identity constraints.

## Acceptance and decision

The deployed scope keeps both sides of each required saved-response check,
fresh reads at every next query, full Build dependency identity, existing task
identities, finite attempts, risk policy, and write-locked submit verification.
Isolated tests cover same-mtime writes between checks, cancellation without a
revision bump, membership add/delete, symlink escape, unrelated corrupted reports,
duplicate authority, ordered recovery chains and captured-result isolation.

The near-term decision is to use the narrow captures in the second batch and the
cost reductions in the first. A future storage redesign requires a separately
reviewable writer inventory, atomic commit protocol, failure matrix and measured
benefit. The current observation must report the full scans that remain; it must
not label an evaluation as an implemented immutable store or a successful GB cut.
