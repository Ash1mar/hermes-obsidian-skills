# Future page execution and read limits

New workflows pin unified-page-execution/v3 and page-budget-index/v2 before
dispatch. Startup and the native model boundary run the same preflight. Missing
request, storage, receipt, recovery or publication support holds execution before
model consumption. Per-page dispatch never falls back to a legacy generator.
Historical pins and request bytes retain their original read-only validators;
this does not migrate or refresh an existing workflow's budget.

Single and two-slot calls use one complete request constructor and PAGE_RULE.
Qualification binds each slot to its own identity, candidate scope, input hash and
source evidence. Textual scope equality, QA prose and context-truncated notices
alone do not prohibit batching. Explicit unresolved candidate QA, missing support,
unproven provenance or special asset transport have concrete recorded reasons.
All conditions, exceptions, contrary context, QA and required assets stay present.
Different complete evidence currently uses independent single-slot requests.

The mutable index contains versions, reservations, page states and hashed payload
pointers. Complete requests/schema, native responses and independent receipts are
immutable files. Responses precede index publication; receipts precede their
index commit. Deterministic orphan discovery replays those interrupted writes
without another model request. Page identity owns at most one initial semantic
attempt plus one mechanical addition; grouping, restarting and recovery cannot
refresh it. Connection-only transport retries retain their separate existing
policy and the same semantic reservation.

Read-only scans and authentication run inside page_budget_store.operation:
one index load and at most one read/hash/parse per immutable payload per operation.
Snapshots grant no authority. Reservation and settlement reread the small index
under the existing transaction lock, check current page state and cancellation
epoch, and atomically advance its version. Stop, permissions and native lease
authority remain live. No persistent or process-global evidence cache is used.

Mechanical acceptance must include actual saved field shapes, unchanged QA and
limits, per-pair reasons, saved-response/no-receipt, partial pair success, interrupted
receipt/index commits, stopped recovery, concurrency10 and fresh budget checks.
Count legacy generation entries (must be zero), index loads, payload loads and
actual byte volume. Complete and partial Build qualification retain their original
native acceptance, QA and dependent-exclusion checks. Isolated/replayed evidence
does not certify a new native two-page success or live cost improvement.
