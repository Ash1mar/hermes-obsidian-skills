# Fixed-conversion downstream workflow tests

This is explicitly cross-workflow/Vault reuse. Subsequent test reruns default to
the same-workflow `start_workflow_test.py --from-node post-mineru` entry described
in `workflow-tests.md`. Missing historical boundaries do not authorize a new Vault.

Use this entry only when the user explicitly requests an independent test reusing
converted material. It is not the ordinary production startup entry:

```sh
python3 "<skill-dir>/scripts/start_post_mineru_test.py" --request "<test-request.json>" --record "<stable-external-receipt.json>" --bootstrap-skill "<bootstrap-skill-dir>"
```

The ordinary startup request is extended by `test_mode: post-mineru-test/v1` and
`reuse_conversion: {vault, workflow_id, sources}`. Each source names `filename`,
`source_sha256`, `bundle_ref` (directory relative to the origin Vault) and an exact
`files` map of every Bundle-relative file to SHA-256, including manifest.json.
The request/record identity pins these values before dispatch. Origins must be
terminal with no native consumers or held at a verified independent short-path
`exact_plan` source handoff with no consumers, runnable cards or live claims.
The latter requires the full conversion scope and verifies the import receipt
and the latched source outcomes. Each Bundle must be a ready source outcome
of that exact workflow. Source originals must match. Symlinks, inventory drift,
changed bytes, conflicting targets and source mismatch fail before dispatch.

The program copies only these conversion Bundles into a new Vault, preserving
QA and all conversion evidence. It retains the original manifest and file hashes
in an immutable test-import receipt. Only the copied manifest's source path and
old governance projection change; the native source worker registers new source
identities, performs ingest-finish, builds UnitSets and validates them normally.
No ready outcome is fabricated. No old Pass, plan, budget, page, review, release
or Provider state is imported. An import validation failure must hold, never
silently call MinerU to conceal the failure. Reentry reuses the pinned receipt;
native-added governance is accepted only for the target Vault, while every other
conversion field/file remains hash-bound.

Measure import/preparation separately from native downstream execution. Omitting
MinerU conversion is outside the tested optimization; do not count that avoided
time as a downstream speedup. Keep the original Vault and Provider namespace
unchanged. A real two-page claim needs one native request, two independently
validated persisted results and two receipts. Current publication and indexing
must use the new Vault's own release, exact scope and Provider namespace.

For a new independent short-path run, use
`python3 "<skill-dir>/scripts/start_post_mineru_short_test.py"`. Supply
`--request`, `--output`, `--provisioner-skill`, `--bootstrap-skill`,
`--provider-config` and `--provider-state`. The preparation request uses
the exact-plan pause solely as a durable handoff from native source
registration and UnitSet validation. The controller does not continue the
legacy Pass graph. It automatically runs the native short mapping, planning
and writing stages, authenticates the completed fragments, then executes
formal Build, eligible partial release, Provider sync and exact-scope
verification. The new Vault must have its own source registration, release
and Provider namespace. A material hold remains visible in the run evidence.
The controller verifies exact declared source identity and applies the existing
scoped source qualification before semantic dispatch. If a completed short
Build reaches a first-release eligibility issue, use the same request and
output with `--resume-delivery` to preserve its exact pages, calls and Build
evidence. A confirmed content issue can be isolated before release with
`--content-isolation <JSON>`, binding the page ID, revision, body hash, cited
source references and reason. Record the interrupted run as an automation
failure even if delivery recovery succeeds.

For a settled legacy mapping failure within the short path, resume the existing
test with `--from-node mapping --reentry-id <id> --mapping-recovery <JSON>`.
The JSON names exact failed request IDs and the completed prior short Build.
If source qualification advanced the registry, provide the original
`--source-registry-revision`; current source hashes and access are still
verified. Original model envelopes and native transcript proofs must match.
Recovered records use a separate versioned receipt. Only affected topic
dependencies reenter downstream stages, and previously committed pages and
known exclusions remain in the base. Full asset inspection and primary
expression are independent of the asset's semantic role.

For failed writing slots with a saved native draft, use the same short test
request and output with `--from-node writing --reentry-id <id>` and
`--fragment-repair <JSON>`. If the current release came from a supplement,
pass its reentry ID in `--prior-entry`. A `revise` change pins the original
job, first reservation and first native response by path and hash. The
controller verifies the transcript, supplies the failed slot and its saved
draft, and keeps verified sibling slots. A complete new page can enter formal
Build and partial publication. If no new page is complete, final acceptance
verifies the existing release and Provider generation and records the gaps.

New writing requests derive coverage identifiers from Markdown citations.
Whole-asset inspections still need explicit acknowledgment. A pure table
wrapper can be accounted for by its supplied, cited and inspected tables.
Unlocated source QA is annotation metadata. An identifiable missing or
unreadable required item still holds its dependent work.

If adding the missing whole-asset attestations would resolve every structural
error, the retry supplies only those assets and accepts only
`inspected_assets`. The program binds the original Markdown to the first
native response and preserves its bytes. A known content omission remains an
independent hold.

For an output-only budget hold, use `--from-node writing --reentry-id <id>`
with `--split-atomic-fragment <fragment-hash>` on a settled test. The splitter
keeps the full evidence, registered tables and relationship graph in each
request. It assigns text requirements and form coverage separately and checks
each complete request and output estimate. An unresolved dependency or an
oversized input remains held. The split stays inside the existing page.
