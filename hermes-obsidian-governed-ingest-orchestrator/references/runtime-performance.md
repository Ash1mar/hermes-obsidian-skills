# Runtime timing and incremental projection

## Current Reduce capacity observation

The user explicitly raised independent Reduce page-body concurrency to a fixed
maximum of 10. `reduce_page_concurrency` is persisted separately from program-worker
and Pass capacity, which remain 2. Earlier concurrency-2 observations below are
historical baselines. Do not auto-tune this setting. Preserve unknown calls and
finite journals, reuse valid results, and stop on sustained ineffective consumption
or a severe shared defect. Acceptance still requires the durable `checkpoint_1`;
this observation does not release Build Finalize.

Reconciliation completion/record reuse is scoped to one cycle and fingerprints
actual dependency contents. Partial revision-covered slices and complex final
stages retain their ordinary proof. A shared parent-node index avoids repeated
whole-graph filtering. Refreshes keep only unchanged terminal native snapshots;
native grant reads and archive subtree discovery remain uncached.

Historical global planning restores its ordered finite journals inside one
authenticated entry/exit. It still reconstructs every full request, all rejected
attempts/corrections, original third-call permission and every cumulative state;
it never trusts serialized progress as an accepted state. Unchanged journals and
receipts are not rewritten, and restored progress is published once. The normal
executor handles unfinished work and unused corrections. Trace
historical_planning_restore and historical_page_restore separately, and record
native worker start, first new SDK request and first persisted body. Offline
completion lookups/validation are not a complete resume latency benchmark.

Recovery measurements must separate read-only historical reauthentication from
full online resume-to-first-request latency. Compare an identical successful
prefix and held set; report excluded native admission, dispatch and publication
work. Invocation-local graph reuse fingerprints current workflow content and
every batch, slice, task and Pass file plus directory membership. It never caches
an execution grant. Native transcript proofs share a read-only connection and
invalidate on database replacement/change or audit/wire changes. At authenticated
entry/exit boundaries, restore proven existing page bodies and sealed holds as a
set instead of replaying per-page write/receipt cleanup. Unchanged finite attempt
journals remain authoritative; neither reuse nor transport changes add calls.

Page input maintenance may reduce duplicate structure with
lossless-page-evidence/v1 for untouched page-write/page-review requests. Compare
the complete outgoing wire, including the decoder instruction and schema; retain
the original for inputs without a net reduction. Never remove original core or
context based on support-ref counts. Preserve canonical input, full text/image
evidence, spans, dependencies, limits and QA; consumed original/encoded journals
retain their exact wire and correction ordinals. Measure actual body input and
correction tokens separately from character-size trials. Review cost/quality
requires genuine Build Finalize work after its durable checkpoint authorization;
partial page drafts do not authorize an early model review lane.

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
   Implemented as `sequential-exact-planner/v2` with `sequential-model-budget/v2`:
   the native worker measures the existing runtime projection and candidate wire,
   plus conservative future-candidate input/output reserves for citation. These
   future-output reserves are explicit heuristics, not predicted model facts or
   a promise that every future response fits. Complete dependencies and image
   authority use the same runtime path; indivisible oversize Units stay blocked.
   New batches pin the existing phase policy. Reader-only library measurement
   remains available; native planning always supplies model-budget measurement.
   The small read-only CLI compares fixed reader limits without writing planning
   checkpoints or changing existing batches. Observing current HBTest2 after
   deployment establishes compatibility/progress, not new-plan task reduction.
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
   Implemented with the existing candidate/Pass schema: compact `name` and
   `finding`, separate applicability/conditions/exceptions, original support refs
   and inspection QA. No hard semantic text truncation or historical rewrite.
   Program-controlled Reduce, page writing and page review restore each active
   citation's immutable original reading package through the runtime renderer,
   with live source/governance/hash/QA and required inspection checks. Source
   handles retain their original spans; shared text is deduplicated per input.
   Full original core and bounded context plus required dependencies participate
   in actual text/image budgets. Oversized evidence blocks the affected request;
   it is never silently replaced with a candidate summary. Old valid records
   use the same deterministic projection without invented semantic fields.
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

## Round-one resume and dispatch completion (2026-10-13)

Resume's first full sync now hands ready program cards to the existing native
CLI before launching the normal watcher. It sends only Vault/workflow identity,
never a permission snapshot. The existing watcher lock serializes this handoff
with any old watcher; a busy lock or failed/partial spawn is reported as deferred
and normal reconciliation remains responsible for recovery. Native claims, live
pause/cancel checks, leases and concurrency 2 remain unchanged.

Within one sync, execution manifests reuse parsed and canonically fingerprinted
content only when newly read bytes have the same SHA. Pointer/workflow/batch
identity checks still run on every call, template bytes are freshly hashed, and
caller-visible plans are copied. The cache expires on operation exit. Journal,
source, actor, revision, pause, cancellation, capacity and lease decisions are
never cached as permission. Staged analyzing constructs only its Pass graph,
without scanning unopened Reduce tasks and Pass history. Later stages retain
their original graph and evidence checks.

Small timeline spans expose desired-graph construction, execution-manifest
parsing, previously unmeasured Gateway/config CLI probes, resume handoff and
watcher launch. Compare resume-to-first-request and completion-to-next-claim
separately from model latency. This is first-round completion, not fifth-round
prefix optimization or a new monitoring/scheduling layer.

## Round-one submission and refill follow-up (2026-10-13)

Keep the ten-second worker lock deadline. Prepare one Pass outside the shared
commit locks, using an operation-local reading snapshot that verifies canonical
paths and current byte hashes. Recheck the current task, sequence, idempotency,
lease, actor, templates, pause and cancellation under the existing write guards
before persistence. Required material inspection and support coverage remain
mandatory. Commit each Pass separately and preserve successful partial receipts.
No permission decision, prepared draft or evidence cache survives this operation.

The watcher first hands existing bindings to the same native CLI, then performs
its normal full sync. Before measuring capacity, the CLI reaps ended native runs
and performs a bounded Pass projection: latch a reached pause, acknowledge a
completed leaf card against its canonical binding and fresh native run/claim/PID,
settle only an exactly ended matching lease, then open existing ready bindings.
Native admission still checks current inputs and capacity 2 before claiming.
Do not create plans or cards, change identities, retry a failed binding or advance
a stage in this projection. Non-leaf cards and historical receipts lacking a
native grant use the normal full projection. Failed worker cleanup sends its wake
hint last; the trusted native reap remains responsible for recording termination.

Observe pass_validation, pass_commit, atomic_pass, pass_refill and native completion
separately. Lock-hold evidence differs from submission time, which includes wait.
Compare completion/termination to the next native claim; signal delivery alone
does not prove timely refill. Keep original results, fixed budgets and pauses.

## Round-five stable input and call-local construction (2026-10-13)

Keep worker and in-flight model concurrency at 2. Preserve the exact system
rules and canonical domain packets. The versioned `semantic-input/v1` wire
places shared material dictionaries before changing task state, uses stable
object encoding, and keeps array order, handles, QA, original text and all
required evidence. Native card identity remains in the native session/audit,
rather than preceding the material prefix. Correction data stays explicit.

One model call prepares its pure wire/content/strict schema and size projection
once. The scope ends in `finally`; it does not reuse agents, sessions, prior
candidate state or an execution decision. Every native request still checks
live worker authority and the exact outgoing text, schema, images, tool
absence and input/image limits. SDK schema copies cannot mutate the prepared
schema. Group measurements reuse only their local schema construction.

Historical native responses retain exact transcript/system/request/response
hashes, real native sessions, current reading authority and same-task content
checks. Recognize only the old `Native task` header and the explicit new wire
header; reject unknown framing or image tails. A stable prefix alone never
authorizes response reuse. Future plan measurement identities include the wire
contract; current task identities and accepted results are not migrated.

Report pure request projection time and call-local construction reuse separately
from agent initialization, historical response reuse and recorded provider cache
tokens. Missing provider cache statistics are unknown, not zero or savings.
No observed cache benefit is a valid outcome. No additional scheduler, persistent
cache, automatic tuning or parallel model calls are introduced.

## Round-one third submission/completion follow-up (2026-10-13)

Public native Kanban show JSON omits the current run, PID and claim owner.
Fast completion reads those fields freshly from the existing native state API,
checks its private key/body against the canonical public card, then compares
the exact durable receipt grant again under the workflow lock. Leaf and pause
guards remain mandatory. Each inspected completed card records its short skip
reason or acknowledged run; omitted public fields never imply authority.

The watcher uses one full-sync attempt per tick. A busy refill returns to the
next fresh refill check; a wake yields the full projection at safe checkpoints
between operations, releasing its dispatch lock. Normal full reconciliation
continues on later ticks and each mutation retains its own live guards. There
is no new scheduling layer or permission snapshot.

Within one submission only, reuse bundled schema parsing/auditing while checking
every record's shape, relationships and invariants. Discard that cache on exit.
Prepared evidence is byte-checked immediately before persistence and again on
snapshot exit; remove the redundant early check before read-only reconstruction.
Keep fresh task CAS, lease, cancellation and native ownership and ten-second
lock timeouts. Measure pass_total, outside pass_validation, actual lock wait and
hold, slice scan, schema, byte checks and persistence separately. Compare domain
completion receipt to native confirmation and subsequent claim; an atomic_pass
drop alone does not prove total improvement. Failed-worker refill remains
unverified without a natural real-run failure sample.

## Round-one fourth full-reconciliation follow-up (2026-10-13)

Optimize the same full-sync path used by resume and the background watcher.
Reuse the bundled schema parsing/audit for this invocation, retaining every
record validation and fresh workflow, task, source, lease and pause check.
Discard the scope even when the sync yields or fails. A status lookup for a
card absent from the active listing uses the same operation's existing show
snapshot; this avoids duplicate archived-card reads. Explicit fresh snapshots
still bypass it, mutations still invalidate projections and refresh promotions,
and native grant/admission checks remain live. No cross-tick permission cache.

Record nonoverlapping state-settlement, capacity, binding and card-projection
phases, schema load/validation counts and completed full-sync outcomes. Compare
resume's first full sync and successfully completed background full syncs
separately. LOCK_BUSY, RECONCILIATION_YIELD, interrupted and cross-deadline cycles
are separate counts, not completed performance samples. Report repeated show
calls per card alongside CLI totals and local phase costs. Runtime counts and
material differ across windows; do not infer costs from the presence of errors
or extend this repair to other efficiency rounds.

## Reduce recovery construction scope (2026-10-18)

The 2026-10-19 same-prefix comparison separates pure packet reconstruction,
native transcript/request proof and accumulated-plan validation from live
authority/admission, dispatch and resume-to-first-request wall time. Keep the
workflow stopped for this read-only comparison; never fake an active binding or
report pure replay time as total resume latency. Successful packet boundaries
may be read from exact native requests and rebuilt once against the current
input and previous state; no greedy trial search is needed for consumed work.
The normal native proof, current authority and full cumulative validation remain.

Reuse the bundled schema audit within one fixed semantic executor invocation and
each standalone authority check. Validate every current record and keep fresh
workflow, pause, binding, source, parent outcome and permission checks. The scope
stores schema parsing only, never an admission decision or graph result. A pure
proposal-to-candidate lookup lasts for the immutable global input invocation and
avoids repeatedly scanning all proposals when constructing related page topics.
Packet order, exact old wires, coverage, budgets and output allowances are unchanged.

Measure packet construction, authority checks and native transcript authentication
separately. Compare the same successful native prefix without extra model calls;
read-only projection time alone does not explain resume-to-first-request latency.
Do not add a persistent cache or progress-based authorization to remove a smaller
cost when repeated schema auditing is the evidenced hotspot.

Live prefix replay also exposed adjacent full authority checks. Global routing
selects its pure packet without another graph check immediately before the
output executor's entry check. The attempt loop delegates saved-response checks
to native_reply, which checks live authority before and after transcript proof;
a new request checks again immediately before reserving its original ordinal,
and the existing native call boundary still checks authority. No allowance,
native proof, source validation or domain submission guard is cached or removed.
Count these remaining authority checks in the runtime timeline rather than
claiming the standalone schema profile as end-to-end recovery savings.
