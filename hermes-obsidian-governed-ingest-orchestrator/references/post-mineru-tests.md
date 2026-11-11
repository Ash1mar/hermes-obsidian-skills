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
registration and UnitSet validation. The controller checks the native Gateway
dispatcher before creating a workflow and leaves the legacy Pass graph paused.
It automatically runs the native short mapping, planning
and writing stages, authenticates the completed fragments, then executes
formal Build, eligible partial release, Provider sync and exact-scope
verification. The new Vault must have its own source registration, release
and Provider namespace. A material hold remains visible in the run evidence.

New native short runs pin the short-card layout before coordination. The same
coordination call groups reader questions into shared topic directories and sees
the names of existing registered directories. The program keeps the stable page
identity separate from the safe `30_Cards/<topic>/<title>.md` path and records
the exact destination in Build. A name collision receives a stable disambiguator.
An already registered identity retains its destination and is held for a
separately authorized revision. Older operation records retain their paths and
writing rules.

The program renders a page title, ordered section headings, a linked contents
list and `knowledge-card` metadata from verified fragments. After release it
refreshes managed `00_目录.md` files from the eligible release subjects.
Navigation is outside the knowledge corpus. An existing user-managed index is
reported as occupied and preserved.

The new writing contract requires primary definitions to be expressed. It
attempts to separate independent evidence components when a local dependency
holds, preserving shared conditions, explicit whole-table dependencies and the
required-fragment gate. Formal proof records source identity, mapping, supplied
materials, required expressions, actual citations, inspection and held scope.
The original page-selection and model correction budgets still apply. Citation
and inspection checks do not certify the semantic accuracy of a paraphrase or
table cell. No independent semantic reviewer is added.

For a same-material comparison, run
`python3 "<skill-dir>/scripts/compare_short_coverage.py"` with `--baseline-vault`,
`--baseline-workflow`, `--baseline-manifest`, and the corresponding `--candidate-*`
options. Manifest paths are relative to their Vault and may select an archived
release. The command validates both registered inventories, verifies published
page body hashes, and lists retained, missing and gained citation locations by
stable material. Use that list for separate checks of the actual definitions,
conditions, exceptions and table details. It is read-only and does not dispatch.
To test from a new Vault and raw PDFs, use the same short-path controller with
`test_mode: bootstrap-short-test/v1` and omit `reuse_conversion`. The native
bootstrap and source workers run fresh conversion. The controller verifies the
exact-plan latch and fresh conversion evidence before admitting the ready
sources to the same short path. Failed sources remain in the handoff audit.
The controller verifies exact declared source identity and applies the existing
scoped source qualification before semantic dispatch. If a completed short
Build reaches a first-release eligibility issue, use the same request and
output with `--resume-delivery` to preserve its exact pages, calls and Build
evidence. A confirmed content issue can be isolated before release with
`--content-isolation <JSON>`, binding the page ID, revision, body hash, cited
source references and reason. Record the interrupted run as an automation
failure even if delivery recovery succeeds.
After a formal Build worker stops without a committed Build result, the same
delivery entry can run one stable formal recovery on the drained board. It
reuses the exact writing checkpoint and authorization. The old card and error
remain auditable, and recovery grants no additional semantic calls.

For a settled short run, use the same request and output with `--from-node
formal-build --reentry-id <id>` to retest the writing-to-Build handoff. A new
native Build card reconstructs the exact original Build from the saved writing
checkpoint and current source proofs. Its committed Build and release must
match the accepted identities. The ordinary release validation, Provider sync
and final acceptance run in order. The identical publication is replayed
idempotently. Evidence goes under `native/reentries/formal-replay-<id>` and
the original acceptance record stays intact. This entry grants no model calls.

For another independent downstream round in the same test Vault, pass
`--repeat-from <first-pipeline-output>` and `--round-id <stable-round-id>`
with the original request and a fresh output directory. The program revalidates
the source handoff and uses a distinct native board and distinct page identities.
Only the source handoff is reused. The round runs fresh semantic work, adds its
eligible page revisions to the verified base release, and verifies its exact new
revisions as well as the cumulative Provider index. Query remains disabled.

For a scoped transport comparison, add `--mapping-from <settled-round-output>`
and `--transport-profile concentrated|paced` to the same-Vault entry. The
mapping cohort starts at the exact native topic receipts and excludes unresolved
reserved packets. Pass `--mapping-fence-from <subsequent-round-output>` for
each stopped comparison with spent unresolved requests. Compare only shared
request IDs when this fence changes the cohort. Keep per-round publication
and the cumulative release separate in the results.

Fixed OpenAI-wire workers record HTTP sends before dispatch and use a
host-local admission journal shared by endpoint, account and model. The shared
default starts at up to 10 in flight and one start per second. The paced
comparison caps at five; concentrated retains the ordinary 10 and permits
simultaneous starts. An overload pauses the route, lowers its cap to five,
and raises it by one after five successes outside cooldown. A confirmed
HTTP 429 without an output or response identity, or a typed failure before
connection, may resend the same request at most twice with at most 120 seconds
of cumulative waiting. Stream overload without such proof stays unresolved.
An available stored response on the direct Responses route may be retrieved
by exact ID. This does not enable response storage or retrieval on other routes.
The SDK's extra send attempts are blocked at the HTTP boundary. Compare
physical sends, `http-sends` records, and accepted coverage independently
from native semantic correction ordinals.

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
# Content outcome recovery and independent section delivery

New readable-card operations pin `content_outcome_recovery`. After writing
settles, the controller authenticates held native receipts and counts actual
slot reservations, including unknown completions and replacement descendants.
A slot with exactly one prior call may receive one targeted content call after
local task scope and explicit registered same-source asset closure are repaired.
Unusable conversion evidence, unavailable required assets and exhausted slots
remain isolated. Valid receipts survive; no retry budget is refreshed.

For an authorized same-Vault writing supplement use
`python3 "<skill-dir>/scripts/start_post_mineru_short_test.py"` with the original
request/output and deployment arguments, `--from-node writing`, a fresh stable
`--reentry-id`, `--prior-entry <accepted-entry>` when applicable, and
`--recover-held-content`. The program creates a hash-bound recovery manifest;
the operator must not author domain results or manually attach evidence.

Incomplete parents remain excluded. A whole planned section with every required
fragment and its explicit dependencies verified may become a narrower readable
card with its own stable identity, title, scope and original native proofs.
Inseparable/comparison plans and incomplete sections cannot be salvaged this way.
Derived cards retain their parent/section lineage in formal Build evidence;
neither the parent nor missing material is counted as covered. Continuous
automation and partial coverage are separate acceptance results.
