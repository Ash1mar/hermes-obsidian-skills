# Typed semantic submission

`native-plugin/ingest-pass-tools/` is a repository-owned Hermes plugin. It registers
ingest_submit_passes, ingest_confirm_citations and ingest_pass_heartbeat through
Hermes PluginContext.register_tool. It does not replace built-in tools, start a
workflow, perform semantic work itself or release a pause. It is available only
inside a native Kanban task with its own checked worker-request.json.

The tool schema is generated from the canonical inspection and candidate types.
The worker authors the actual findings, conditions, exceptions and evidence refs.
The backend owns actor, task identity, registry/package revisions, template hash,
idempotency keys and Pass fingerprints. Wrong types are rejected before renewal;
unknown refs, input drift and authorization errors fail through existing guards.

## Installation when deployment is authorized

Install the six synchronized Skills and this new plugin from the same recorded
Git SHA. Copy the plugin directory to the host's Hermes plugins directory as
ingest-pass-tools. Enable it in each worker profile's plugins.enabled and add the
ingest_pass toolset to the profile if its tool selection is restricted. Configure
plugins.entries.ingest-pass-tools.settings.skills_root to the deployed domain
Skills directory. The default is skills/domain beneath the plugin's Hermes home;
profiles sharing another installation must set the explicit configuration.
Use the existing deployment rules: quiesce consumers, record old/new remote SHAs
and fingerprints, integrate host-only changes, retain no old-code backup.

After loading, inspect native tool availability and actual registered schemas.
Do not assume copying a plugin enables it in every profile. The CLI fallback is
fully supported but does not give the model a native typed tool schema.

Worker templates in existing workflows are immutable snapshots. A new install
does not update them. On a cancelled Pass workflow, the authorized operator uses
the existing execution amendment path to select the new compact worker contract
for unfinished work, preserving valid preparation, plan and completed outcomes.
Amendment and resume remain separate authorized operations. Installation alone
is not native acceptance or authorization to resume.

## Submission and evidence

ingest_submit_passes accepts vault and passes. Each Pass has task, input_id,
sequence, inspections, candidates, empty_reason. Conditions, exceptions and
support_refs are arrays; kind and qa are constrained enums. The input_id is the
observed presentation identity, not a field to fabricate. One Pass per task per
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
{"confirmations":[{"task":"t1","input_id":"<observed input_id>",
"candidate_pass_id":"<persisted candidate ID>","decision":"confirmed_unchanged",
"review_note":"<actual bounded evidence/logic/QA review>"}]}
```

Use the exact candidate ID returned by submission or continuation.candidate.
The backend copies the verified candidate's inspections, QA qualifications,
candidates and empty_reason into the first citation, validates all ordinary
contracts, and saves a linked citation-reviews record. Confirmation never happens
merely because JSON is identical. It is not an independent review or permission
to skip reading. Changed semantics and later revisions use full authored Passes.
CLI equivalent is batch-confirm-citations with the same bound arguments.

Both native submission tools renew/save the current checked binding internally.
Use ingest_pass_heartbeat while reading between submissions. CLI Pass heartbeat
without --request-output refreshes the input file only if it is a safe regular
file in the native workspace; an unsafe target is rejected before lease mutation.
Stopped/expired/stale bindings are not automatically reconstructed or retried.
The old validate-only API remains for diagnostics, not a required extra model turn.

Native acceptance records the real tool/CLI invocation, successful validation
receipt, canonical Pass, native completion and durable stage pause. Regression
tests or a successfully installed plugin alone do not satisfy native acceptance.
