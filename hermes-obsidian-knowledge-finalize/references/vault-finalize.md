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

## Internal retrieval governance

`python3 "<skill-dir>/scripts/manage_vault_finalize.py" <vault> preflight --request <json>`
calculates current qualification with the same rules as planning; it does not
write a plan or start a Provider. Counts distinguish delivery, index eligibility,
missing business/visibility/QA conditions and exact isolated pages.

Page revisions retain their immutable draft governance fields. An explicitly
authorized native program planning worker can append `hermes-page-governance/v1`
under the fixed `internal-vault-retrieval-policy/v1` policy. The policy approves
source-grounded retrieval only inside the named Vault, not professional
applicability or public access. It binds content/version/hash, current qualified
sources, supporting Passes, exact delivery closure, Build acceptance, any required
native review, actor and authorization. Missing records retain the old defaults.
There is no blanket promotion and no new semantic review. QA is never cleared:
unresolved pages and dependents are recorded and excluded from the new snapshot.
Original content, reviews,  delivery gaps and previous releases stay intact.

Plans/releases optionally pin `page_governance`; planning, apply, validation and
Provider admission interpret the same effective conditions. Changed content or
evidence invalidates the binding. Scope, source and authorization failures remain
closed. A new governance decision requires a new release, never an edited old
manifest. Provider code must support this contract before a later authorized sync.

Release operations reuse bytes/JSON only within one controlled calculation and
rehash actual inputs before commit. Workflow permissions and leases never enter
this cache. Independent verification uses a fresh calculation. Out-of-band code
or data maintenance must stop consumers. Telemetry separates calculation,
confirmation, JSON parsing, bytes, unique captured files and reuse counts; these
instrumented reads do not include separate operator audits or uninstrumented
filesystem operations. No cross-operation cache or concurrency increase is used.
