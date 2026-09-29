# source-prepare · v7

Prepare only the source assigned to this card. Run its complete `worker_command` unchanged once using terminal `background=true, notify=true`. The helper reads a canonical dispatcher binding and performs begin/check/complete internally. Do not build request JSON or call begin/check first. If the command is missing, stop with a dispatch contract error.

Poll the same process in short calls until exit. Hermes maintains native heartbeats. The helper supervises conversion, checks explicit binding and cancellation, reuses validated current SourceUnits or the workflow's own attempt output and verified repair Bundle, and permits at most one supported conversion retry. Derived Bundles live under `_system/reports/source-bundles`; never annotate or overwrite `10_Raw`. Unresolved QA warnings remain unresolved.

Return resource/UnitSet IDs and QA/coverage evidence from the result. Failed execution stops the card; the trusted reconciler archives the binding and holds dependents. Never invoke Hermes Kanban CLI, retry a failed binding, clear isolation markers, patch bindings or ledgers, resume/cancel the workflow or reinterpret runtime/contract errors as source damage. No neighboring sources, planning, Pass, Reduce, Finalize or checkpoint approvals.
