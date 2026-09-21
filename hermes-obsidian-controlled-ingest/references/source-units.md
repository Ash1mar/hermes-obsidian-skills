# P3 Source-unit and knowledge-build operations

Use this path only when `_system/vault.json` declares `phase: P3`,
`capabilities.source_reader: true` and `capabilities.knowledge_build: true`.
It runs source preparation through Build Finalize. Vault Finalize, Provider sync
and query remain unavailable.

The runtime is embedded under the Skill `lib/` directory and uses Python 3.11+
standard library only. All paths passed to the commands below are Vault-relative.

## Prepare a normalized artifact

For governed Bundle v2 after `ingest-finish` has added `manifest.governance`:

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  prepare-bundle --bundle "10_Raw/converted/example_bundle"
```

For immutable UTF-8 Markdown already under the Vault, supply the identity already
registered for the source:

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  prepare-markdown --source "10_Raw/example.md" \
  --document-id doc-example --version-id version-example-1 --resource-id resource-example-1
```

Preparation normalizes CRLF/CR to LF, generates or imports an outline, copies
declared Bundle assets into an immutable artifact revision, hashes every stored
component and returns the artifact-manifest path. It does not publish units.

## Preview and publish

Preview and build use identical generation. Read the resource's current pointer;
use revision `0` when none exists. A mismatch is a conflict, not permission to
overwrite.

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  preview --artifact-manifest "_system/sources/artifacts/<resource>/<revision>/manifest.json" \
  --actor "<actor>" --expected-revision 0

python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  build --artifact-manifest "_system/sources/artifacts/<resource>/<revision>/manifest.json" \
  --actor "<actor>" --expected-revision 0
```

Build publishes `manifest.json`, `sections.json`, `units.jsonl` and `engine.json`
under `_system/sources/units/<resource>/<unit-set>/`, then atomically updates
`current.json`. Published revision directories are immutable. Repeating the same
build is idempotent; an historical unit set cannot silently become current again.
The engine report records the effective canonical configuration, document profile,
attempted/rejected strategies, selected strategy, size/coverage statistics, token
audit status, protected-structure exceptions and other diagnostics.

## Read and validate

`list` and `validate` accept a resource and optional unit-set ID. `get` reads a
JSON SourceRef; `context` reads a JSON array of SourceRefs. Reads require the
current document-registry revision and recheck artifact/unit hashes. Context expands
within the unit's section and its ancestor sections under a codepoint budget. It
does not enter sibling child sections, and returned context never counts as
inspected core evidence.

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  validate --resource-id resource-example-1

python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  audit-tokens --resource-id resource-example-1 --max-tokens 1024
```

Text coordinates are LF-normalized Unicode codepoint, zero-based half-open spans.
Source units are the canonical chunks shared by knowledge construction and the
future Provider. They use the source overlap configuration without crossing a
section owned range. Heading breadcrumbs and context are separate.
The packaged starting values are 512 target, 1024 maximum and 80 overlap
codepoints. Changing strategy, size or overlap produces a new UnitSet; Provider
model or rendering changes do not.
Oversized fenced code, formulas and tables are preserved and reported rather than
silently truncated. Unknown pages or image regions remain unknown.

The packaged `audit-tokens` command uses the explicitly named Unicode-codepoint
diagnostic counter; it exercises the adapter path but is not a model tokenizer.
P5 must inject the actual embedding tokenizer and immutable fingerprint.

## Pass/Reduce and Build Finalize

Use `manage_knowledge_build.py` after publishing UnitSet v2. Requests are JSON
files so shell quoting cannot alter content or exact references.

1. `plan --request task.json`, then `claim`, creates and revision-checks a bounded
   `knowledge_build` task. Active overlapping tasks are reported; they are not
   silently merged because separate tasks may intentionally consume the same Unit.
2. `read` persists a `hermes-reading-package/v1` containing core/context roles,
   actual text or asset metadata, hashes, omitted refs and budget truncation.
3. `pass --request pass.json` records Pass 0 candidates followed by contiguous
   Pass 1..N citations. Every support ref must lie inside an explicitly inspected
   material. Context may support a candidate after inspection, but it does not
   complete the task's target coverage.
4. `reduce --request reduce.json` consumes citation Pass candidates, requires full
   target inspection and no deferred range, then stages one immutable page revision
   per stable subject. Identity keys are explicit judgments; aliases and similar
   names never auto-merge subjects.
5. `finalize --request finalize.json` checks task/build/identity revisions, draft
   and parent hashes, exact provenance and one review per page before any write.
   It then commits page files, revision sidecars, identity registry and task state.
   The run manifest is written last, so an interruption can be safely retried.

Zero-candidate work still records Pass 0, complete inspections and a substantive
Reduce reason; it can Finalize with no page or identity. A committed page remains
`business_status: unassessed` and `visibility: draft`; QA is independently
`usable` or `qa_required`. P4 Vault Finalize will determine release projections.

### Batch orchestration

For more than one bounded task, prefer the additive batch commands. They retain
the task, reading-package, Pass, build-run and page-revision contracts above; the
batch ledger only records orchestration state under
`_system/ledgers/knowledge-build-batches/<batch-id>.json`.

- `batch-measure --request <json>` performs a read-only preview of each task's
  exact canonical reading projection. It reports core, ancestor, context,
  metadata and serialized codepoints plus pinned input/config fingerprints.
- `batch-plan --request <json> --exact-reading-budget` validates all UnitRefs,
  active-task overlaps and exact reading budgets once before creating ordinary
  task ledgers. Only fitting tasks are written; indivisible text assets report
  `WHOLE_ASSET_OVERSIZE` instead of being truncated.
- `batch-adopt --request <json>` attaches an existing plan after read-only task,
  origin-hash and SourceUnit validation. It never rewrites an adopted artifact.
- `batch-set-state --request <json>` preflights explicit actor/revision-pinned
  blocked/failed/skipped transitions, including superseded active plans.
- `batch-status` and `batch-resume` report the authoritative recovery point.
  Use `batch-status --compact` for aggregate counts and next actions without a
  task-by-task payload. Slice counts and completed/total slice coverage are
  included in the compact result.
- `batch-next-slice --batch-id <id> --worker-id <id>` initializes deterministic
  Pass slices when needed and atomically leases one ready slice. The defaults are
  concurrency 2, at most 3 tasks and 30000 serialized input codepoints per slice,
  a 1800-second lease, a 60-second heartbeat cadence and 3 attempts. An optional
  config JSON pins different values when the first slice is initialized.
- `slice-heartbeat` extends a live worker-owned lease. `slice-complete` and
  `slice-fail` consume revision-pinned JSON requests; stale, expired, foreign or
  cancelled leases cannot publish late results. Retryable failures enter
  `retry_wait`, then become ready when due; exhausted or non-retryable failures
  become blocked.
- `batch-pass` derives a stable idempotency key from batch ID, task ID, Pass
  sequence, verified reading-package fingerprint and the slice-pinned template
  hash. An exact replay returns the existing Pass, while different content under
  the same key fails with `IDEMPOTENCY_CONFLICT`. A changed task revision,
  package fingerprint, registry pin, slice assignment or template hash fails as
  `STALE_INPUT`. Include `slice_id` and `worker_id` so a worker-owned lease is
  verified before new semantic output is accepted.
- `slice-fail` classifies rather than blindly trusting a retry flag. Rate limits,
  timeouts and process exits use 60/180/600-second exponential backoff; rate
  limits also pause new leases with a batch-level cooldown. Invalid model JSON
  requires `failed_output`, stores it under the batch failure directory and gets
  at most two retries. Stale input enters `reconcile_required`; whole-asset,
  contract and provenance failures block; checkpoint failures enter
  `awaiting_approval` without retry.
- `slice-reconcile` is restricted to the batch actor and a revision-pinned
  reading-budget failure. It remeasures through the canonical reading path,
  cancels the superseded slice and creates deterministic replacements. A
  replacement carries `reslice_count: 1`; another budget failure blocks it.
- `batch-reclaim-expired` returns expired leases to ready or blocks them after
  the final attempt. `batch-cancel` preserves completed slices and atomically
  cancels every unfinished slice. Cancellation is terminal in this contract.
- `batch-prepare` claims pending tasks and persists one bounded reading package
  per task. For exact plans it first reruns the same measurement and returns
  `STALE_PLAN` before claiming when the reader configuration, pinned UnitSet
  revisions or serialized projection changed. Repeated calls reuse a matching
  package.
- `batch-pass --request <json>` records model-produced Pass requests in task
  sequence. It schedules semantic work; it does not replace evidence judgment.
- `batch-reduce --request <json>` writes ordinary draft runs after rejecting
  duplicate task ownership, output paths and stable identities across the batch.
- `batch-validate` requires every non-blocked task to belong to exactly one
  validated run before setting checkpoint 1.
- `batch-finalize --request <json>` preflights all explicitly approved runs and
  commits them serially under the existing global Finalize lock. Each legacy run
  remains independently idempotent and recoverable.

Use one model reading context per bounded task or small related group. Batch does
not mean concatenating every reading package into one prompt. For cross-source
knowledge, perform local candidate work first and provide globally reconciled
identity/path decisions to `batch-reduce`. Never cross a human checkpoint merely
because a batch command can continue.

Slice ledgers live at
`_system/ledgers/knowledge-build-batches/<batch-id>/slices/<slice-id>.json` and
are the authoritative scheduling records for Pass workers. They pin task IDs,
input/template fingerprints, attempt, revision, lease timestamps, results and
last error. Ordinary task, reading-package and Pass records remain the
authoritative knowledge-build records; a completed slice result reference does
not itself create or validate a Pass.
