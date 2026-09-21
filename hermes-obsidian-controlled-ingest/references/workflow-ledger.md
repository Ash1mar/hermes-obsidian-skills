# Ingest workflow ledger (phase 5)

`scripts/manage_ingest_workflow.py` manages one authoritative record at
`_system/ledgers/ingest-workflows/<workflow-id>.json`. This phase does not
dispatch Hermes/Kanban workers and never reports background execution as running.
`rebuild-kanban` persists deterministic *desired* Pass-slice nodes; its
`board_id` remains null until a later adapter materializes them. Kanban state
must never replace the Vault workflow or knowledge-build ledgers.

Commands: `start`, `status`, `resume`, `approve`, `cancel`, `reconcile`,
`rebuild-kanban`, and `digest`. All mutations are JSON `--request` inputs with
`workflow_id`, `actor`, `expected_revision`, and `input_digest`. The digest is
`sha256:` plus the canonical-json SHA-256 of every request field except
`input_digest`; `digest --request <file>` computes it before submission.
`start` requires revision 0 and a caller-stable `ingest-...` ID, making an
identical retry safe. Adoption by `batch_id` pins the existing task set and
registry revision without rewriting reading packages, Passes, or the batch.
A new workflow without a batch starts at `created`; the transition from
`planning` to `analyzing` supplies the newly planned `batch_id` and pins it.

The ordered stages are `created`, `source_preparing`, `planning`, `analyzing`,
`reducing`, `checkpoint_1`, `build_finalizing`, `release_planning`,
`checkpoint_2`, `applying`, `indexing`, `validating`, then `completed`,
`partial`, or `failed`. `reconcile` advances one stage only; it is a control
plane transition, not an execution command. Downstream transitions require
existing Vault artifact references. Checkpoint 1 additionally requires a
validated batch in `checkpoint_1` with draft runs. Each checkpoint freezes an
approval digest over pinned inputs and artifact references. `approve` must
present that exact digest at the current checkpoint before a subsequent
`reconcile` can advance. The second checkpoint similarly gates `applying`.

`cancel` stops new workflow scheduling but does not delete completed domain
artifacts. `resume` reads an active workflow's authoritative recovery point;
for a cancelled workflow it explicitly restores the previous stage at a new revision.
Domain batch/slice cancellation and lease recovery remain separate operations;
the Kanban adapter in phase 6 must coordinate them and re-check the workflow
cancel flag before worker writes. `status --compact` omits task maps and
artifact arrays. Profiles control display only; they do not change stages.
