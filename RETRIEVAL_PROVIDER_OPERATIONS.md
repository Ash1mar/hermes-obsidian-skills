# Release-driven retrieval Provider operations

qmd-like-rag 0.5 is deployed separately from Hermes and the six Skills. The Vault contains
canonical SourceUnits, knowledge releases and a portable retrieval status record. Chroma, BM25,
model/tokenizer assets, caches and generation locks stay on the Provider host.

## Configuration layers

1. Repository Skill adapters:
   - Query: `hermes-obsidian-controlled-query/config/retrieval-provider.json`
   - Release writer: `hermes-obsidian-knowledge-finalize/config/retrieval-provider.json`
2. Deployed Skill copies under the active Hermes Skills root.
3. Provider-host configuration such as `/root/.config/qmd-like-rag/main.json`.

Query and release-writer adapters have independent `enabled` switches. Disabled means the Skill
must not start the Provider, read host model configuration or load model/index libraries. Query is
always read-only. Only `sync_release_index.py`, run after a committed Vault Finalize release, may
call Provider sync.

Both checked-in production adapters default to disabled. An explicit one-time experiment uses
the native `probe_release_provider.py` entry point and a request binding the current release,
paused workflow, adapter hashes, model configuration and external audit directory. The request
must declare sync_once, preserve_pause and keep_disabled. Dispatch with
`python3 "<orchestrator-skill-root>/scripts/probe_release_provider.py" dispatch --vault "<vault>" --request "<request.json>"`.
It creates a real native program task, runs the supported Finalize writer with a temporary enabled
configuration and `--no-write-manifest`, then removes that temporary configuration. It does not
resume the workflow, replace its disabled status, or enable Query. The resulting host index may
be tested through an explicitly requested direct recall; regular query routes stay disabled.
Observe the Provider status/progress separately, retain timings and native receipts, and verify
the original pause and adapter bytes afterward. No automatic second attempt is admitted.

## Readiness sequence

1. Install qmd-like-rag 0.5 in its independent environment or container.
2. Pin embedding/reranker models and both publisher tokenizer assets. Record their actual
   SHA-256 values; do not rely on null placeholders in example configuration for production.
3. Run `qmd-like-rag doctor` and `status`. Confirm `tokenizers`, Chroma and BM25 are available and
   embedding and reranker tokenizer readiness are true.
4. Commit a knowledge release with `manage_vault_finalize.py ... apply`.
5. Enable the Finalize adapter and run:

   ```bash
   python3 "<finalize-skill-root>/scripts/sync_release_index.py" <vault-root>
   ```

6. Confirm `_system/reports/retrieval-index-manifest.json` is schema 2.0, ready, names the current
   release ID, has a non-empty generation, exposes SourceUnit capability and reports a ready
   tokenizer.
7. Enable Query only after a direct recall returns complete projection fields and UnitRefs.

The first 0.5 sync creates a fresh generation. Do not copy or migrate the old Markdown-chunk index.

## Main

Use command transport to the WSL-native installation. The tested layout keeps the virtual
environment under `/root/.venvs/qmd-like-rag`, the stable CLI at `/usr/local/bin/qmd-like-rag`,
host configuration under `/root/.config/qmd-like-rag/`, and generations under
`/root/.local/state/qmd-like-rag/<vault-id>/`. Local models and tokenizer files must resolve from
the immutable model snapshot in offline mode.

## Intranet

Use the same tagged source in a separate CPU container and approved remote embedding/reranker
services. Mount `/opt/data/phq/testVault` read-only. Bind Provider state to a host path such as
`${HERMES_HOST_DATA_ROOT}/phq/qmd-like-rag-state`; do not put it below the Vault. Join Hermes and
Provider through the deployment's external Docker network.

Build and scan the image outside the disconnected environment with pinned dependency versions,
tokenizer assets and checksums, export it with its image digest, and import it. Configure HTTP
transport only with an explicit deployment endpoint. Never infer a service URL.

## Failure handling

- Missing or mismatched tokenizer: sync fails; never substitute another tokenizer.
- Submitted release differs from the current release: sync fails without publishing a generation.
- Embedding, renderer or embedding-tokenizer fingerprint changes: create a new generation;
  SourceUnit IDs remain. Reranker and query settings are audited independently and do not rebuild
  vectors under `release-index/v2`. Legacy generations retain their original strict checks.
- Provider unavailable during Query: record the route as unavailable and continue hierarchical
  navigation; do not build or repair an index.
- Failed sync: preserve the previous `last_success`, leave the knowledge release committed, and
  keep `query_ready` false for that release.

## Sync progress and proportional recovery

Use `qmd-like-rag status --vault-root <vault> --config <host-config>` (or HTTP `GET /status`)
to observe `sync_progress` while a sync request is in flight. The selected generation's status
and the current attempt's status are distinct. A failed attempt does not promote its candidate;
a ready older generation does not make a newer release query-ready.

The Provider bounds vector windows by `index_batch_size`, `embedding_batch_max_tokens` and
the storage limit, with smaller model calls controlled by `embedding_batch_size`. Keep model
concurrency unchanged. Metrics identify computed, cached, deduplicated and resumed inputs and
time spent in each phase; report measured scope instead of assuming a speedup percentage.

Within existing execution authorization, retry the same release ID/hash after correcting a
transient failure. Verified completed batches are reused after current input/provenance checks.
A name-only remote model does not qualify for cross-release vector caching. Never edit cache,
checkpoint, release or pause records to obtain readiness. A stale release must be resubmitted
through the supported writer; a damaged selected generation requires an authorized `--rebuild`.

Changing code, inspecting status or completing isolated checks does not authorize Provider sync,
workflow resume or crossing `release_sync`. Native workflow stage evidence remains separate
from Provider implementation verification.
