# Typed semantic submission

`native-plugin/ingest-pass-tools/` is a repository-owned Hermes plugin. It registers
bound input, submission, heartbeat and completion tools through Hermes
PluginContext.register_tool. Separate operator-only tools inspect and resume/cancel
existing workflows through supported dispatch. It never replaces built-in tools,
starts a new workflow, authors domain semantics or releases a pause.

The tool schema is generated from the canonical inspection and candidate types.
The worker authors the actual findings, conditions, exceptions and evidence refs.
The backend owns actor, task identity, registry/package revisions, template hash,
idempotency keys and Pass fingerprints. Wrong types are rejected before renewal;
unknown refs, input drift and authorization errors fail through existing guards.

## Installation when deployment is authorized

Install the six synchronized Skills and this new plugin from the same recorded
Git SHA. Copy the plugin directory to the host's Hermes plugins directory as
ingest-pass-tools. Enable it in each worker profile's plugins.enabled and add the
ingest_pass toolset to the worker profile if its tool selection is restricted.
Add ingest_workflow to the trusted operator's toolset. Worker contexts cannot call
these operator tools. Configure
plugins.entries.ingest-pass-tools.settings.skills_root to the deployed domain
Skills directory. The default is skills/domain beneath the plugin's Hermes home;
profiles sharing another installation must set the explicit configuration.
Use the existing deployment rules: quiesce consumers, record old/new remote SHAs
and fingerprints, integrate host-only changes, retain no old-code backup.

After loading, inspect native tool availability and actual registered schemas.
Do not assume copying a plugin enables it in every profile. Legacy bound CLI
operations remain supported for their explicit contracts; an execution failure
in a native worker does not authorize switching to CLI or reconstructing binding.

Worker templates in existing workflows are immutable snapshots. A new install
does not update them. On a cancelled Pass workflow, the authorized operator uses
the existing execution amendment path to select the new compact worker contract
for unfinished work, preserving valid preparation, plan and completed outcomes.
Amendment and resume remain separate authorized operations. Installation alone
is not native acceptance or authorization to resume.

## Submission and evidence

Native workers prefer ingest_begin_pass(vault) to obtain their checked binding and
readable authorized material in one call. Source text appears once, while every
task retains its evidence handles, roles, spans, QA and assets. Long input and
candidate identities remain in the checked backend snapshot, not the model packet.
No model-written Python expansion or canonical-package reads are needed. The
presentation is checked against the slice's actual codepoint budget.

Successful receipts include pass_kind and next_action: a candidate needs actual
review; a successfully authored citation is complete and must not be submitted as
an original candidate to the confirmation tool. Native receipts are compact; their
receipt_ref preserves the complete backend result for audit. The existing verified
source cache now covers semantic expansion as well as persistence; freshness,
permission and lease checks still run.

Use ingest_complete_pass after required Passes are durable. It returns verified
counts and a full audit receipt reference. End the worker immediately; the trusted
reconciler owns native acknowledgement and dispatch. Do not repeat completion or
compute decorative statistics after the workspace is cleaned.

Operators use ingest_workflow_status(vault, workflow_id?, runtime?) for compact
authoritative counts. runtime=true adds a single native task-list summary rather
than fetching every card and log. For explicitly authorized actions use
ingest_control_workflow(vault, workflow_id, action=resume|cancel, operation_id).
It obtains current actor/revision and creates the digest and supported request.
Saved requests/results bind the stable operation_id; replay returns its receipt,
while an unknown interrupted outcome requires observation rather than blind
redispatch. Resume cannot release a persistent pause. CLI bridge equivalents use
python3 "<orchestrator-skill>/scripts/native_ingest.py" --action status|control
with a UTF-8 JSON request on stdin; do not reconstruct private identity fields.

ingest_submit_passes accepts vault and passes. Each native Pass has task,
sequence, inspections, candidates, empty_reason. Conditions, exceptions and
support_refs are arrays; kind and qa are constrained enums. Omit input_id: the
program supplies it from the native worker's original verified input snapshot.
One Pass per task per
submission; batch candidates before citation review. Bound CLI equivalent:

```bash
python3 "<ingest-skill>/scripts/manage_knowledge_build.py" --vault "<vault>" \
  --worker-binding "$HERMES_KANBAN_WORKSPACE/worker-request.json" \
  batch-submit --request "<typed-draft.json>"
```

The single guarded call performs current validation once for each new record and
checks the lease again immediately before committing. It persists a preflight
receipt linking the semantic request, expanded draft, Pass ID, package fingerprint,
template and checked lease revision. Receipt existence proves validation; actual
Pass existence and matching ID prove persistence. A crash between these writes
does not mean the Pass completed. Retry the identical supported request after
checking current state; successful replay preserves the existing Pass.
No externally supplied "validated" flag can skip freshness or authorization.

After actual review, ingest_confirm_citations accepts vault and confirmations:

```json
{"confirmations":[{"task":"t1","decision":"confirmed_unchanged",
"review_note":"<actual bounded evidence/logic/QA review>"}]}
```

The program binds the original candidate actually shown in the input or persisted
by this worker's submission. The model supplies no candidate_pass_id. Neither
identity is silently refreshed from newer evidence. Legacy explicit identity
fields remain accepted only when they match this observed snapshot; the bound CLI
retains its original explicit-identity contract.
The backend copies the verified candidate's inspections, QA qualifications,
candidates and empty_reason into the first citation, validates all ordinary
contracts, and saves a linked citation-reviews record. Confirmation never happens
merely because JSON is identical. It is not an independent review or permission
to skip reading. Changed semantics and later revisions use full authored Passes.
The legacy CLI is batch-confirm-citations with explicit checked identities.

Both native submission tools renew/save the current checked binding internally.
Use ingest_pass_heartbeat while reading between submissions. CLI Pass heartbeat
without --request-output refreshes the input file only if it is a safe regular
file in the native workspace; an unsafe target is rejected before lease mutation.
Stopped/expired/stale bindings are not automatically reconstructed or retried.
The old validate-only API remains for diagnostics, not a required extra model turn.

Native acceptance records the real tool/CLI invocation, successful validation
receipt, canonical Pass, native completion and durable stage pause. Regression
tests or a successfully installed plugin alone do not satisfy native acceptance.

## Native execution outcomes

Native tools distinguish `waiting`, `recovery_required`, `stopped`, and
`draft_rejected`. Only `draft_rejected` / `correct_semantic_draft` permits one
bounded semantic correction; successful records from a partial batch stay valid.
Other states return `end_worker` and a trusted recovery owner. Do not repeat begin,
reconstruct request/descriptor files, search implementations, or use CLI fallback.

Admission refusal preserves the underlying reason (`concurrency_limit`,
`slice_leased`, `batch_cooldown`, or `slice_not_ready`) and parks the exact native
binding with a durable execution report. It does not steal or clear another lease.
A leased worker lacking bounded input is failed through the supported adapter,
releasing only its checked lease. Changed input and interface failures preserve
Pass records and request trusted recovery; no source reset or automatic semantic
retry is authorized. Existing workflow pauses remain enforced.

`native-inputs/` stores the worker-owned original packet identity and observed
candidate IDs. Canonical packet manifests, ordered task ownership, source hashes,
live preflight, lease checks and Pass fingerprints remain authoritative. A model
sees short task/material handles; original long identities and complete submission
receipts remain in the backend audit records.
