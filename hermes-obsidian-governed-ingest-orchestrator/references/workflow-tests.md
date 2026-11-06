# Default test reruns: same workflow

Use `python3 "<skill-dir>/scripts/start_workflow_test.py"` for test reruns. It keeps
the physical/logical Vault, workflow and startup identity. Ordinary startup/resume
never erases results or grants a test cycle. No new-Vault mode is offered here.

| Entry | Restart scope |
| --- | --- |
| `bootstrap` | Revalidate the same scaffold, then source preparation; no new Vault |
| `source-prepare` / `mineru` | Native source preparation, including conversion when needed |
| `post-mineru` | Reuse accepted Bundles, registration and UnitSets; restart at `exact-plan` |
| `exact-plan` | Exact planning and downstream work |
| `canary` | Canary, remaining Pass and downstream work |
| `pass` / `map` | Remaining Pass, retaining completed canary results |
| `resource-reduce` / `reduce` | Source Reduce and downstream work |
| `global-reduce` | Global planning, page work and downstream work |
| `checkpoint-1-validate` | First checkpoint and downstream work |
| `build-finalize` | Build/review and downstream work, retaining native page bodies |
| `vault-finalize-plan` | Publication planning and downstream work |
| `checkpoint-2-validate` | Second checkpoint and downstream work |
| `release-apply` | Apply the approved release and downstream work |
| `provider-sync` | Provider synchronization and final acceptance |
| `acceptance` | Final acceptance and native completion settlement |

An entry identifies a parallel node family, not one arbitrary card amidst running
siblings. The completed prefix keeps native identities, results, full evidence,
conditions and QA. Only restarted work gets new native card identities.

For the first authorized test startup, set `test_mode: same-workflow-node-test/v1`
in the short startup request to enable boundary capture. Dedicated post-MinerU
test startups and an admitted historical Reduce test also enable future captures.
Installing code does not enable/change a paused existing Vault or authorize work.
Only test security domains with Query disabled qualify.

The scheduler captures a stable boundary before first native readiness of each
node family, after prefix consumers and slice leases end. Hash-bound Vault-data
blobs are deduplicated across boundaries/cycles; installed code and host Provider
indexes are excluded. Captures grant no execution authority. Telemetry reports
file counts, logical bytes, new blobs/bytes and duration separately so capture
overhead remains visible in efficiency comparisons.

```bash
python3 "<skill-dir>/scripts/start_workflow_test.py" --vault "<vault>" \
  --workflow-id "<existing-workflow>" --list
python3 "<skill-dir>/scripts/start_workflow_test.py" --vault "<vault>" \
  --workflow-id "<existing-workflow>" --from-node "<node>" \
  --operation-id "<stable-operation>" --plan --request "<request.json>"
python3 "<skill-dir>/scripts/start_workflow_test.py" --vault "<vault>" \
  --request "<request.json>" --dry-run
python3 "<skill-dir>/scripts/start_workflow_test.py" --vault "<vault>" \
  --request "<request.json>" --record "<external-receipt.json>"
```

List/plan/dry-run make no model requests or dispatch. Execution requires separate
authorization and a stopped/terminal isolated `auto_full` test workflow without
staged pause policies. Never use this to release an HBTest2 stage pause.
Missing historical boundaries block explicitly, with no fabricated snapshots or
automatic cross-workflow fallback. The historical Resource Reduce entry may use
the existing exact saved-input/Pass proof described in `reduce-tests.md`.
`start_reduce_test.py` is a compatibility alias; existing pinned requests still work.

A journal retires authorized old managed data and restores the selected boundary.
It finishes an interrupted admission idempotently. Repeating an admitted operation
never clears results or grants more calls; later cancellations/pauses stay held.
Original sources and unrelated user notes cannot be silently overwritten.
Checkpoints, publication qualification, release/sync and acceptance still use
normal native contracts. Query remains disabled until release/index coverage agrees.

Pass capacity is pinned from current configuration (currently10). Existing batches
use the supported stopped capacity operation, preserving slice/result identities;
replanned batches initialize with the pinned capacity. Historical capacity2 notes
cannot override the request.

An authorized retest differs from zero-call recovery. Prefix budgets stay intact;
retired downstream calls/responses/receipts remain for cumulative accounting. New
cycle costs are reported separately and cumulatively. Restart/recombination within
a cycle never resets page allowance or resends a valid page.

`start_post_mineru_test.py` remains an explicit cross-workflow/Vault conversion
reuse entry for independent targets. Subsequent tests default to the same-workflow
entry, including `post-mineru`. Missing historical boundaries never silently select
the cross-workflow path. Deployment/documentation authorize no live test or sync.
