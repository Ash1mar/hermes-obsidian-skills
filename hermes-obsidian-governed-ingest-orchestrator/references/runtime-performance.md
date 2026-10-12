# Runtime timing and incremental projection

Runtime diagnostics live on the execution host under
`~/.cache/hermes-skill-runtime/timelines/<vault-and-workflow-key>/`.
They are observations and wake hints, never permission or workflow state.
Per-process JSONL spans contain workflow, native task/run, node where available,
start/end timestamps, monotonic elapsed time, parent span and error code.
Model audits link their model-call span; request construction, SDK call,
materials, binding expansion, atomic Pass persistence, projection, native claim
and completion acknowledgement can be compared separately. Lock diagnostics
retain lock wait/hold measurements. Do not add nested spans or lock holds to
their parents; compare interval unions and exclusive time instead.

Each native projection uses one operation-local task listing and reads attempt
history only when the listing indicates a previous start (unknown fields take
the conservative full-read path). Native mutations refresh the listing because
completing or archiving a parent can promote other cards. The snapshot expires
at operation exit. Actual claims still use live admission checks and native CAS;
permissions, source signatures, leases, cancellation and revision are not cached
as authorization. Passes retain individual atomic writes and partial recovery;
immutable material parsing is shared only within the submission invocation.

Finished workers send a host-local wake hint to the exact matching watcher.
The watcher reconciles durable outcomes and refills native capacity; periodic
checks with bounded idle backoff remain the recovery mechanism. The watcher
continues to use Hermes' native CLI runtime for process registration. No new
model calls, task regrouping, semantic contract or concurrency changes are made
by these mechanical optimizations.

For a bounded observation, record resume-to-first-request latency, CLI operations
per projection, per-Pass persistence cost, model busy-time union, effective
completions and partial records, cancellation drain, and material-volume/quality
limits of the comparison. Keep operator overhead separate from native semantic
work. Preserve verified completed and partial results and every durable pause.

Explicit `blocked_reason` slots are held with their original native audit and
refusal receipt, without an unchanged-material correction call. Valid slots and
other groups still use the normal submission and citation checks; unresolved
slots prevent slice completion. An explicit refusal is not an invalid JSON
response and is not by itself proof of model misjudgment. Schema/contract errors
retain their existing bounded correction path.

During the current efficiency observation, repeated unreadable OCR or missing
table reports from a known image-PDF conversion are deferred source limitations.
Do not reconvert or revise each item automatically. Check registered assets and
authorized packet/coverage to distinguish source loss from a usable required
asset omitted by the program. The latter remains an implementation issue.
Retain valid results, refuse incomplete downstream evidence, and stop repeated
useless calls. Known local conversion limitations alone do not require stopping
unrelated work. Round-two grouping must preserve complete authorized text,
images, schema and correction budgets; worker concurrency remains 2.

Required dependencies are derived by the program from explicit registered local
asset links, table identifiers or exact numbered table references in bounded
core text. Section-wide associations alone are not dependencies. The program
resolves same-source whole SourceUnits with live governance and hashes, keeps
original reading packages and Passes immutable, and persists a hash-bound
reading supplement with its core-to-dependency links. Required context uses
normal material handles and transport; it does not expand core coverage. New
Passes require its inspections; unavailable or oversized material stays held.
Historical results without required dependency inspections cannot pass Reduce
merely because the file is structurally complete. Codex does not manually add
worker materials or generate semantic findings.

The `phase-balanced/v2` budget policy is explicitly pinned, including its
configuration hash, through a supported execution amendment. An absent pin
keeps legacy grouping; a mismatched pin rejects execution. Candidate estimates
include core and required text plus inspection/image allowance. Citation uses
prior candidate size, source review scope and revision allowance, rather than
assuming confirmation. These byte-based estimates are labeled as heuristics;
input limits remain codepoints, model output limits remain tokens. Actual
instructions, structured schema, QA, text, image labels/count/bytes and bounded
correction reserve are checked. A contiguous partition minimizes call groups
before balancing utilization, without truncation or cross-slice permission.
Grouping audits record utilization and input/output/image/boundary reasons.
Budget amendments preserve blocked slices and all valid existing results; they
do not authorize unchanged source or semantic retries.
