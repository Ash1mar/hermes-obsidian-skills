# Profiles

Profiles tune severity without changing the underlying rule codes.

## post-ingest

Use after controlled ingest or batch ingest.

- `qa_required` is warning/info depending on context.
- `pending` is warning.
- `in_progress` and `stale` are errors.
- bundle validation `fail` is error.
- weak QA boundaries and unstructured multi-source synthesis are warnings.
- authoritative promotion of QA-affected evidence is error.
- engineering governance `readiness: draft` is warning.

## query-ready

Use before controlled query work.

- missing citation contract is error for single-source knowledge cards and spec indexes.
- open QA is warning because it affects answer authority.
- missing source-map or ledger alignment is error.
- weak QA boundaries remain warning; authoritative promotion is error.

## strict

Use before publication, archive, or handoff.

- open `qa_required`, `pending`, `in_progress`, or `stale` states are errors.
- weak QA boundaries and unstructured multi-source synthesis are errors.
- authoritative promotion of QA-affected evidence is error.

## qa-review

Use to plan manual QA.

- open `qa_required` is info unless the ledger is stale or the bundle fails validation.
- grouped QA metrics matter more than pass/fail.
- weak QA boundaries are warnings so reviewers can prioritize missing safeguards.
- authoritative promotion remains error because it hides the QA state.

## Governance readiness

All profiles validate an enabled engineering control plane with the same stable rule codes. Invalid IDs,
schemas, references, states, paths, or version relationships are errors. `readiness: draft` is a warning
for `post-ingest`, `query-ready`, and `qa-review`, but an error for `strict`. A Vault without
`_system/vault.json` remains in `legacy` governance mode. Candidate source organizations follow the
same severity rule as draft readiness.
