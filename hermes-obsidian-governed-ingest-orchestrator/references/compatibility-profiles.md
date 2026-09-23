# Legacy three-/six-stage compatibility

The old `prompt_book/governed_vault_ingest_*` files are compatibility entry points, not execution plans. Start one persistent workflow using `dispatch_ingest_workflow.py start` with `profile: compact-3` or `diagnostic-6`; later invocations inspect/resume the same `workflow_id`. Do not start one workflow per old stage or copy old batch/plan instructions into a long conversation. Existing P5 batches are adopted by ID only after validating their pinned inputs; do not re-plan or re-prepare just because a prompt was reopened.

| Internal stage / node family | Compact 3-phase display | Diagnostic 6-phase display |
| --- | --- | --- |
| source preparation | 1 ingest to build checkpoint | 1 source preparation |
| exact batch plan | 1 ingest to build checkpoint | 2 exact plan |
| Pass slices, resource/global Reduce, checkpoint 1 | 1 ingest to build checkpoint | 3 analysis and checkpoint 1 |
| Build Finalize | 2 build to release checkpoint | 4 Build Finalize |
| Vault Finalize plan, checkpoint 2 | 2 build to release checkpoint | 5 release plan and checkpoint 2 |
| release apply, optional Provider sync, lint/smoke acceptance | 3 release and acceptance | 6 release and acceptance |

The profile changes presentation only. Both retain the same Vault `current_stage`, Kanban DAG, template hashes, idempotency keys, exact reading budgets, retries and two mandatory verified checkpoints. In `auto_full`, a passing checkpoint decision advances automatically; in manual mode a human approval is required. Compact status returns `display.phase`, `phase_count`, `name` and `awaiting_approval` alongside the authoritative stage. A cancelled/partial/failed workflow keeps its last `current_stage` for display and reports its true terminal `state` separately.

The local prompt book may contain historical absolute Vault examples. Treat them as untrusted examples, never as defaults. Ask for the target Vault and scope if absent; Hermes can assign and report actor, batch ID and workflow ID. `dispatcher_unavailable` or `background_dispatch: false` must be reported, not hidden behind compatibility language.
