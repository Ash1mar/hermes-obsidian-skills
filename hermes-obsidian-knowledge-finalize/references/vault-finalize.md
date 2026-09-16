# Vault Finalize contract

Vault Finalize consumes completed Build Finalize manifests and the current
SourceUnit/knowledge identity repositories. It does not reconstruct knowledge from
Markdown filenames.

The plan request contains `release_id`, `actor`, `expected_state_revision`, an
optional exact `build_run_ids` list, optional `source_changes`, and a reason. When
`build_run_ids` is omitted, unapplied completed builds are selected. A replacement
pins both old and current UnitSet IDs. A withdrawal pins the old UnitSet and uses a
null current ID.

Contribution results have these meanings:

- `current`: every cited contribution still belongs to its resource's current UnitSet.
- `review_required`: at least one contribution is stale or withdrawn, while another
  current contribution still supports the page.
- `blocked`: no current source contribution remains. The page is retained and kept
  out of index eligibility.

Moving a stable page may create a redirect only when the old file still matches its
committed revision or an earlier generated redirect. Finalize never overwrites
unrelated content. Wikilinks resolve through exact path, stem, canonical name or
alias; missing and ambiguous targets block apply.

The release manifest pins Build revisions, identity registry revision, source
changes, affected dispositions, navigation hash, redirects and the complete index
eligibility projection. It is written last. A missing manifest means the operation
did not commit and `apply` can be retried with the same request.
