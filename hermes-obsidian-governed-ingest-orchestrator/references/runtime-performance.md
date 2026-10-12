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
