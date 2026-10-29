# Coordinator fresh observations and protected content reuse

## Narrow predicates

`_failed_card` and the Pass branch of `_execution_blocked` retain their original
fresh workflow observations. Each obtains a verified read-only document, never
the outer reconciliation's older value. First matching task-map entry wins.
Failure report checks, slice checks and boolean short circuits keep their order.
Only the chosen binding is copied. An incomplete slice consults revision headers
in original history/current order; coverage materials are projected lazily only
for a qualifying revision. No protected-files list is copied by these predicates.
Mutation APIs continue to return isolated, mutable full documents.

This step eliminates full copying at the two predicates, not repeated reads of
the archived storage segments. Measure those costs independently. Existing
completion-proof and native projection caches are retained.
