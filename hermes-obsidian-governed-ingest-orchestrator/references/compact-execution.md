# Compact Pass inputs and execution amendments

This operator action changes how remaining Pass tasks are packaged and presented.
It keeps the same workflow, batch, task IDs, target SourceRefs, exact-plan measurements,
source outcomes, accepted Passes and completed native bindings. It does not reset sources,
replan target windows, resume work or release a pause. It applies generically to a cancelled
Pass workflow whose canary is already accepted and dispatch policy is `full`.
The ordinary pinned worker pack remains the default for an unamended workflow.

## Evidence and model presentation

Canonical reading packages and Pass records remain authoritative. `worker-begin` verifies
their identities, live source/asset bytes, registry access and QA, then persists a separate
model view. The view retains material text, core/context roles, deduplicated heading titles,
bounded source spans, truncation limits and applicable QA diagnostics. History is selected
by the next required step: no Pass narrative before candidate, the original semantic
candidate for a missing citation, and only effective refs/revision relations for an already
complete task. Full candidate/citation/inspection history stays in canonical audit records.
Every diagnostic remains unless an explicit valid range proves it unrelated. Unknown QA
restrictions remain visible. A registered whole binary asset gets its exact authorized path;
linked assets are labelled as unread and never imply access to their contents.

One returned model packet shares identical text, heading titles and QA restrictions across
its tasks. Each task retains its own `m1` material references, role, source span, linked/whole
asset permissions and input ID. Shared `c1` text and `q1` QA handles cannot be cited as
SourceRefs, and identical text in different documents does not merge their evidence identity.
Workers see short task labels `t1` and material refs `m1`. Full UnitRefs, hashes, package
identities and revision bookkeeping remain in the backend mapping. They supply all semantic
inspections, candidates, conditions, exceptions and citations. `batch-pass --compact`
expands only checked handles within the bound slice, verifies the input ID against current
evidence, then invokes the existing canonical Pass validator and writer. Out-of-window spans,
foreign handles, changed evidence and stale leases remain errors. Candidate and citation
drafts are batched separately; a partial task appends only its missing Pass. Preview and
write use the identical draft and produce identical Pass IDs. Receipts omit the full batch
task list and evidence mapping.

Packing counts the shared packet, including QA and required continuation context, and the begin
receipt. Preview reserves envelope space; begin measures its actual JSON presentation and
fails if the configured cap is exceeded. It never counts body text alone or discards necessary
metadata to make a task fit. This is a codepoint cap for the returned input; static instructions,
later generated candidates, tool schemas and multimodal model token usage are separate costs.
The task-level canonical reader limit and fingerprints remain unchanged.

## Operator procedure

Obtain explicit authorization to revise execution. Inspect current status, pause/revision
history, raw/source/QA identities and native attempts. Use supported cancel if necessary.
No task may retain a Vault lease and no native attempt may be running or dispatchable.
Finish an outstanding semantic revision review first. Do not release a canary to satisfy
this prerequisite without its separate continuation authorization.

Prepare one immutable request in the operator workspace:

```bash
python3 "<skill-dir>/scripts/prepare_execution_amendment.py" --vault "<vault>" \
  --workflow-id "<observed-id>" --amendment-id "<stable-operation-id>" \
  --reason "<authorized packaging change>" --max-tasks 12 \
  --max-input-codepoints 30000 --output "<operator-workspace>/amendment.json"
python3 "<skill-dir>/scripts/dispatch_ingest_workflow.py" --vault "<vault>" \
  amend-execution --dry-run --request "<operator-workspace>/amendment.json"
```

The request carries current actor/revision, complete mutation digest and installed compact
worker content. Dry-run writes no Vault domain or execution-plan records. Review pending
slice count and input budget, preserved completed slices, unchanged task IDs and partial
tasks before applying the same request without `--dry-run`. These are adjustable packing
parameters, not hardcoded Vault, source or task identities. The prepare helper defaults to
at most 12 tasks and 30000 codepoints; actual shared size may split earlier. Concurrency and
lease policy are preserved. Increasing the cap does not
expand any worker's canonical material window.

Apply records an immutable execution generation and a protected evidence hash manifest.
Only unfinished tasks get new slice/native identities. Superseded cancelled slice files and
their native history remain audit evidence; active dispatch uses the batch's new slice list.
Completed bindings, canary membership/acceptance, continuation history, scope, pauses and
valid domain records remain unchanged. The workflow and batch remain cancelled.

An interrupted write leaves a journal and blocks resume/dispatch. Retry the exact saved
request, not a freshly generated digest. Recovery verifies the journal, protected records and
fresh model evidence, then finishes that generation. A changed request or changed evidence
is rejected. A completed operation replay only acknowledges its existing generation; it
does not roll back a later amendment. Never repair an interruption by editing ledger files.

Resume the same workflow only when separately authorized. New native workers read the
returned packet path, submit bound compact preflights/writes and complete their ordinary
durable tasks. Existing completed workers do not rerun. Preserve every configured pause:
all Pass completion still holds Reduce until explicit `continue-workflow` authorization.
Review actual native calls and artifacts, not model claims or isolated test outputs.

An explicitly authorized semantic revision of a completed compact slice uses the existing
revision worker and canonical bounded package. It must not interpret a completion-only
summary as permission to skip the new revision. Historical supersession remains intact.
