# One short request through Final Acceptance

For a request to create a Vault and ingest specified files, resolve the Bootstrap
Skill through the runtime loader, then invoke:

```bash
python3 "<orchestrator-skill-dir>/scripts/start_vault_workflow.py" --request "<request.json>" --record "<host-run-receipt.json>" --bootstrap-skill "<loader-bootstrap-skill-dir>"
```

The trusted operator translates the short prompt into a JSON object with
`operation_id` (stable lowercase ID), `actor`, absolute `vault_path`, stable
`vault_id`, display `vault_name`, `security_domain`, and absolute `sources`.
The operation ID also determines `ingest-<operation_id>`. Preserve the request
and receipt on retries. New targets must be empty; existing Vault ingestion
continues through the existing workflow entry, without bootstrap.

The CLI selects the installed Hermes Python before any Vault mutation. An
interrupted initial handoff resumes only the same active, unpaused startup;
cancelled, failed, terminal and latched-paused workflows are observation only.
The receipt remains pending until the actual coordinator process and its matching
ownership marker are observed. A live coordinator is reused, never duplicated.
An already completed startup receipt does not authorize a fresh resume.

Optional `execution_policy` overrides the four positive integer limits:
`pass_worker_concurrency` (default 10), `source_prepare_concurrency` (2),
`program_worker_concurrency` (2), `reduce_page_concurrency` (10).
The effective values are fixed in the receipt and workflow scope. Native host
capacity and memory pressure still apply. Pass capacity is separate from other
program workers; Reduce body concurrency is separate again.

`canary_policy` defaults to `sample`: select up to eight slices from the complete
plan, deterministic source-diverse first round, then plan order. Selection and
input hashes are immutable. Small batches run all slices once. `off` explicitly
omits the trial, retains ordinary Pass validation, and cannot have a Canary pause.
Old workflows without the field retain their eight-slice contract.

`pause_after` defaults to an empty list for a request explicitly authorizing
continuous execution through publication and Final Acceptance. Preserve any
user-requested stops. Both automatic checkpoints remain mandatory. Governance,
required evidence and finite execution allowances still block affected work;
continuous execution never approves unknown authority or invents coverage.

Optional `retrieval_policy` is exactly:

```json
{"contract":"vault-retrieval-policy/v1","sync_enabled":false,"query_enabled":false}
```

Enable sync/query only within the user's scope. The portable policy lives at
`_system/metadata/retrieval-policy.json`; the workflow pins its hash. Commands,
HTTP endpoints, model paths and credentials remain in host adapter configuration.
Host `available:false` is an unconditional kill switch. With no Vault policy,
the existing host `enabled` behavior is preserved. Query remains read-only and
must independently validate current release/index readiness. Policy changes
during a bound workflow fail closed and require explicit supported handling.

The entry validates bootstrap identity and scaffold, imports immutable originals
with hash checks, writes a stable startup report and creates one native workflow.
An external receipt handles interrupted bootstrap-to-workflow handoff. A retry
observes an existing workflow; it does not release a pause or resume a cancellation.
Bootstrap validation failure remains held rather than overwriting partial data.

The compact-5 display is: bootstrap, source ingest, knowledge build, build
finalization, release and acceptance. Bootstrap is represented by the startup
receipt/report; workflow status exposes phase 2 onward. Within knowledge build,
`subphase` distinguishes exact_plan, map_pass, reduce and reduce_validation.
The internal DAG, checkpoint evidence and trusted native completions remain.

Multiple Vaults use separate workflow bindings, boards and output trees under one
Hermes installation. The ingest-program capacity lock is shared by Kanban root,
while each workflow retains its own limits. Generic Gateway/model tasks still
share the native host budget: this change is not proof of fairness or of atomic
admission against an unrelated third-party dispatcher. Live multi-Vault evidence
must be obtained separately before claiming that deployment is fully qualified.
Updating shared installed code requires all consumers to stop; pinned templates
alone do not pin executable bytes. Cancel only the selected workflow and retain
shared Gateway services needed by others.

For an explicitly authorized change to an existing Pass batch, stop and drain the
workflow, then call `python3 "<orchestrator-skill-dir>/scripts/set_pass_capacity.py"
--vault "<vault>" --workflow-id "<id>" --limit 10 --operation-id "<stable-id>"`.
The atomic batch pointer names a hash-bound append-only request. Original slice
configuration, input fingerprints, Canary selection, attempts and domain outputs
remain unchanged. This operation neither repairs failures nor resumes execution.
Native status reports `pass_runtime_capacity`; host capacity still limits admission.
