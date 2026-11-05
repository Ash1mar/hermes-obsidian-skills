# Fixed-conversion downstream workflow tests

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
terminal with no native consumers, and each Bundle must be a ready source outcome
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
