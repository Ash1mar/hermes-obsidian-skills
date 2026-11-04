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
