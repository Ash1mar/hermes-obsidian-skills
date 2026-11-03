# Automatic bounded Pass recovery

Continuous `auto_full` workflows default to `pass_failure_policy=once_then_isolate`
in `config/orchestration.json`. `hold` disables automatic recovery. Staged and
canary-only workflows retain their explicit operator boundaries.

The native reconciler hands a drained blocked Pass stage to the program recovery
controller. It authenticates ended native sessions, request/response hashes,
current reading inputs and canonical task ownership. Each task/material/phase has
a durable call budget: one initial request plus at most one correction. Changing
workers, regrouping tasks or restarting the program does not reset that budget.
Existing native calls are adopted only with authenticated evidence.

After the correction fails, the controller uses the supported exclusion operation
for the unfinished tasks, preserves completed tasks and every original response,
and writes an auditable coverage-gap report before resuming independent work.
Partial Canary coverage is reported as partial, never as successful eight-slice
acceptance. Excluded work is unavailable to Reduce and cannot be published.

Conversion defects, changed or absent required evidence, permissions, transport
failures and unknown native completion do not gain an unchanged-material retry.
Unauthenticated failures stay held. Recovery cannot cross a durable pause.

`scripts/recover_ingest_workflow.py` is the same controller used automatically by
the watcher; it can also continue a maintenance-stopped workflow without selecting
individual incidents. Invoke with `python3 "<skill-dir>/scripts/recover_ingest_workflow.py"
--vault <explicit-path> --workflow-id <id>`. It authors no domain output.
