# Workflow maintenance contracts

New short requests save `scope.recovery_policy` before Bootstrap. It includes
QA isolation, at most one additional semantic check, one source resource retry,
three mechanical controller attempts, unknown-completion hold and scoped initial
release qualification. `semantic_failure=hold` disables semantic auto recovery;
`initial_release=hold` disables initial qualification. Neither setting changes
QA evidence validity. Existing workflows and startup receipts are not migrated.

MinerU's shared local supervisor admits one conversion at a time. Source card
concurrency remains two; Pass, Reduce and other capacities remain unchanged.
A terminal typed CUDA OOM diagnostic, bound to source hash and backend, permits
one supervised resource retry in a fresh derived directory. The reservation is
durable before execution and shared across both backends. Original evidence is
retained. Unknown completion, environment errors, PDF load/quality failures and
OCR problems do not enter this retry. A crashed reservation with no final
diagnostic stays held. HTTP conversion keeps its deployment transport.

The watcher reserves a recovery episode before handing off to the supervisor.
The supervisor survives its child's exception/exit and spends its saved bounded
mechanical budget. Domain controllers retain the existing stable cancel,
recovery and resume identities. Retrying cannot allocate another semantic
attempt. User cancellation changes the operator-stop epoch under the workflow
lock, preventing that episode from resuming. Explicitly reinvoking
`python3 "<skill-dir>/scripts/recover_ingest_workflow.py" --vault "<vault>" --workflow-id "<id>"`
discovers one unfinished episode; `--episode` can name the recorded identity.
Held/exhausted episodes are not automatically replenished. No scheduled or
reboot service is installed. An uncertain operator receipt remains held.

Initial release qualification occurs before the first native planning attempt
for new Bootstrap workflows. The program holds dispatch, drains through the
supported control operation, creates a deterministic draft preview, resolves
closed source declarations and seals exact Bootstrap/source/Build authority.
The native plan worker still performs page qualification and ordinary plan
validation. No model request, QA clearance, invented applied release, Provider
scope change or pause release is granted. Other blockers stay held. Historical
failed-plan recovery remains supported for old ledgers.

Continuous Pass workflows use the same native-grant-checked fast acknowledgement
as staged workflows. The completion wake hint grants no authority. The existing full
projection and terminal settlement remain fallbacks. A timeout is separately
recorded from native-grant release and is not proof of native completion.

## Read-only metric collection

```sh
python3 "<skill-dir>/scripts/report_workflow_metrics.py" \
  --vault "<vault>" --workflow-id "<id>" \
  --native-db "<hermes-home>/state.db" \
  --output "<external-new-report.json>"
```

The report contains the 100-ID registry, a current ledger snapshot, historical
telemetry windows, known native usage and explicit missing samples. S04/Z01
always refer to the current snapshot. A terminal domain state does not imply
that native consumers drained. Existing historical reports are never overwritten.
The report records whether the ledger revision changed during collection.

Optional `--windows <json>` supplies a list of `{window_id,start,cutoff}` using
Unix wall-clock seconds and half-open intervals. The default historical interval
includes pauses and maintenance and is not a steady-state performance benchmark.
Span distributions use recorded monotonic durations and include failures
separately. SDK intervals are paired by session, phase, process and clock domain;
ambiguous pairs remain unknown. Nested spans are never summed as workflow time.
Token totals cover recorded unique workflow sessions, not window-local usage.
Missing usage is counted, cache reads remain an input subset and currency cost
stays unknown. Query quality is not inferred from a ready index.

Optional `--protected-baseline <json>` accepts `{absolute_file_path:sha256}` and
reports changed/missing/unchanged files within that explicit scope as W07.
Unlisted new files are not checked. Provider data is read from the portable
manifest and matched to the current release bytes; no model, index load or
Provider sync occurs. A ready manifest is reported as recorded state, not fresh
verification of host index contents.

## Efficiency collection before the next native run

Collector version 2 reads each timeline once, streams JSONL and reuses audit and
receipt bytes within its invocation. `efficiency` contains call-to-receipt and
durable-attempt links, logical read/hash/parse/cache costs, cache pre-hit scans,
acknowledgement outcomes, task/run/clock-matched completion chains, committed
stage windows, phase activity, recovery records and process writer overhead.
Existing registry IDs remain stable; several entries reference these sections.
Nested shared read ledgers are counted once. Repeated path access is not labelled
redundant without compatible version and authority evidence. Raw disk I/O,
exact native claim/lease transaction times and physical HTTP retries remain unknown.

New native requests carry a request ID through model/SDK spans and the existing
audit. Actual fixed-message sections are counted during their existing rendering;
SDK payload fingerprints reuse the existing boundary hash. No full prompt copies,
extra model calls or document rereads are introduced for these measurements.
`input` is an exact combined material/metadata section, not a guessed semantic
breakdown. Character/byte sizes are not token usage. Provider-envelope size and
unavailable token detail are not inferred. Successful persistence, native grant
release and timeout have distinct evidence. Release-to-next-worker intervals
alone do not prove eligible idle capacity or wasted time.

Only correlation fields propagate into child spans. Large parent read counters
are not copied into every child event. Writer totals are cumulative per process
and max-selected, not summed; they exclude the final write and include other
workflows in that same process. Metrics are best-effort observation, never an
execution or retry authority. No polling or periodic whole-Vault scan was added.

`--observations <json>` accepts an external list with `event_id`, `vault`,
`workflow_id`, `kind` (observation/controller_reentry/manual_repair/maintenance),
and observer-provided time/evidence fields. A missing list means unknown manual
interventions, not zero. `--protected-root <path>` together with the explicit
hash baseline inventories new files in that scope; protection hashing is an
explicit final audit cost, not part of the normal telemetry loop.

After successful native terminal settlement the watcher attempts one external
final report under its host timeline directory. Export failure is observable and
never reopens domain work. This automatic report uses available native usage,
but operator annotations and explicit protection checks require the external
read-only export above. Existing historical reports and old workflows are not
rewritten. Missing new fields in old runs remain unknown. Pass remains 10 and
MinerU remains 1; no recovery budget, pause, QA rule or deployment default changes.

## TestVaultE2E efficiency maintenance (2026-11-04)

Baseline: `tmp/testvault-e2e-20261104/completion-summary.json` and final metrics.
120 unique observed SDK requests (119 returned, one unknown), 36 published / 7
excluded pages, terminal partial. List/show: 461/351. Schema: 5,090 logical reads,
552,754,850 bytes; one review packet: 74 reads, 80,461,680 bytes. These are logical
reads, not physical I/O. Operator maintenance prevents a clean wall-time comparison.
Pass stays 10; MinerU stays 1; QA isolation and at most one additional check remain.

Order implemented: request/audit accounting and effective audited recovery policy;
native read-only board lists and nested projection reuse; immediate reconciliation
on revision progress; operation-local installed schema observations; immutable
review-input reuse inside existing release calculation with final hash confirmation;
exact RR QA dictionaries; unique Candidate material inspection slots and exact
missing-reference diagnostics; evidence-block classification; derived page evidence
manifests; bounded offline two-page preview.

Five investigated additions: Candidate pass-0001/pass-0004 duplicate inspections;
pass-0003 uninspected support; Resource Reduce unsupported c35 example; page-write
candidate/evidence identity mismatch. Free-text refusal explanations are not trusted
reference substitutions. RR permits supported proposals plus explicit omissions.
The 15-input side investigation estimated JSON 305,795 -> 239,529 characters before
extra decoder instructions. This is a baseline hypothesis, not token/time savings.

Presentation is frozen per workflow. Existing audits, output journals or reserved
Pass budgets select legacy presentation; new work gets efficiency-presentation/v1.
No old reading package, Pass, response or allowance is rewritten. Schemas are cached
only within an operation with fresh inode/size/mtime/ctime observations; deployment
must stop code consumers. Live authority/leases/cancellation remain uncached.

Page evidence currently falls back to full related packages when relevance cannot
be mechanically proved. The derived manifest records exact support, retained refs
and fingerprints. No reduction in original evidence is claimed. Two-page pairing
is an offline preview only, requiring identical nonempty source scope, complete
low-risk pages and a complete request budget. It grants no native dispatch; live
pairing remains pending an independent per-page durable budget/receipt contract
and explicit future experiment. No real model or workflow is used for maintenance.
