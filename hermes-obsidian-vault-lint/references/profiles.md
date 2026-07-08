# Profiles

Profiles tune severity without changing the underlying rule codes.

## post-ingest

Use after controlled ingest or batch ingest.

- `qa_required` is warning/info depending on context.
- `pending` is warning.
- `in_progress` and `stale` are errors.
- bundle validation `fail` is error.

## query-ready

Use before controlled query work.

- missing citation contract is error for single-source knowledge cards and spec indexes.
- open QA is warning because it affects answer authority.
- missing source-map or ledger alignment is error.

## strict

Use before publication, archive, or handoff.

- open `qa_required`, `pending`, `in_progress`, or `stale` states are errors unless a future waiver mechanism records an explicit exception.
- warnings from `post-ingest` should generally become errors.

## qa-review

Use to plan manual QA.

- open `qa_required` is info unless the ledger is stale or the bundle fails validation.
- grouped QA metrics matter more than pass/fail.
