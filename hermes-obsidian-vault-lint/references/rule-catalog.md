# Rule Catalog

Rule codes are stable identifiers for automation and reports.

## Vault Structure

- `vault.missing_path`
- `vault.missing_required_dir`
- `vault.missing_governance_file`

## Markdown Frontmatter

- `frontmatter.missing`
- `frontmatter.missing_field`
- `frontmatter.unexpected_type`

## Bundle Integrity

- `bundle.validator_unavailable`
- `bundle.validation_failed`
- `bundle.validation_warning`
- `bundle.missing_control_file`

## Ledger and Source Map

- `ledger.invalid_json`
- `ledger.unknown_status`
- `ledger.in_progress`
- `ledger.stale`
- `ledger.pending`
- `ledger.qa_open`
- `ledger.output_missing`
- `source_map.missing`
- `source_map.mismatch`

## Evidence Contract

- `evidence.missing_field`
- `evidence.unknown_bundle`
- `evidence.unknown_section`
- `evidence.not_recorded_in_ledger`

## Multi-Source and QA Boundary

- `synthesis.multi_source_unstructured`
- `qa.boundary_weak`
- `qa.authority_overpromoted`

`synthesis.multi_source_unstructured` requires a Markdown evidence table with `bundle`, `section`, `pages`, and `owned lines` or `source lines` columns when two or more Bundle v2 ids are cited. Row bundle/section pairs are checked against section ledgers.

`qa.boundary_weak` applies when an artifact cites a `qa_required` or warn/fail-quality section but has neither `evidence_level: needs-qa` nor an explicit QA/review boundary in its body.

`qa.authority_overpromoted` applies when QA-affected evidence is paired with `evidence_level: clear|source-backed` or artifact status `approved|authoritative|final|published|verified`.
