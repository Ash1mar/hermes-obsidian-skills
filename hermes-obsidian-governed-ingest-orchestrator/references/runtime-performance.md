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

Cancellation can replace a slice's last error with a worker shutdown diagnostic.
Execution amendments and resume therefore also consult the original semantic
failure receipts bound to the same batch, slice and input fingerprint. They
hold current ownership containing unresolved refused tasks, keep completed
ownership and valid partial Passes, and resume unrelated slices. A receipt for
different inputs cannot freeze a slice. This mechanical preservation does not
classify the refusal as a model mistake or repair domain content.

## Narrowed five-round plan (2026-10-12)

Keep already implemented changes. Round two finishes phase-specific conservative
budgets, ordered grouping, fixed reserves, split-reason audits, configuration
fingerprints and valid partial results. Defer **adding** automatic tuning,
complex task reordering or persistent old-task merge/migration. Existing
contiguous balancing is retained. Performance percentages are observed metrics,
not targets that require expanding scope. Required material supply is program
correctness; conversion limits and occasional model errors stay deferred.

1. Round one retains measured hotspot fixes. Measure resume-to-first-request,
   response-to-submission and worker-completion-to-next-start. Fix evidenced
   repeated scans or waits without a new monitoring platform, persistent
   cross-process cache, service or event bus. Timely refill belongs here.
2. Round two completes the bounded scope above and its authorized deployed
   verification and observation. No extra concurrency or tuning framework.
3. Round three plans **future new tasks** by adding whole Units in source order,
   measuring their actual projection and output reserve, and closing a window
   before the next complete item exceeds budget. Remove the fixed eight-Unit
   starting point. Required context, tables, formulae and images participate;
   indivisible oversize material remains explicitly blocked. Reuse one planning
   and measurement path with a small read-only parameter comparison. Keep
   current HBTest2 identities and outcomes; no migration, replacement workflow
   or rebuild of its current plan to test this algorithm. Native validation of
   a new plan requires separate authority for that plan.
4. Round four first shortens candidate restatement using existing fields:
   short rule/topic, original references, necessary conditions/exceptions or
   cross-paragraph explanation, and uncertainty/source limits. Citation retains
   factual, condition and omission review. Then make Reduce and page review
   retrieve original evidence with necessary context. Extend a versioned
   contract only if existing fields cannot suffice. Preserve old valid records
   through limited deterministic compatibility; do not invent missing semantic
   information. No evidence graph, automatic topic hierarchy, sentence network
   or bulk candidate migration. Measure total Map, Reduce and review cost when
   those stages are authorized; Map-only observations cannot prove total savings.
5. Round five is a small prefix/local-cache optimization: stable serialization,
   stable rules/schema/material prefix and operation-local parsing/construction
   reuse. Report actual cache statistics or unknown status, retaining strict
   historical native-response reuse. Worker and in-flight model concurrency stay
   at 2. No new scheduler or worker-internal parallel calls. No observable cache
   benefit is a reportable outcome, not a reason to build new infrastructure.

Each implementation follows authorized main commit, scoped deployment, brief
post-deployment checks, same-workflow bounded observation and reliable stop,
then normal main-to-intranet maintenance. Keep durable pauses, accepted outcomes
and source identities. Future-round implementation is not automatically
authorized by documenting this plan.
