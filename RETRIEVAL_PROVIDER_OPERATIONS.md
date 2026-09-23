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
- Model, renderer or tokenizer fingerprint changes: create a new generation; SourceUnit IDs remain.
- Provider unavailable during Query: record the route as unavailable and continue hierarchical
  navigation; do not build or repair an index.
- Failed sync: preserve the previous `last_success`, leave the knowledge release committed, and
  keep `query_ready` false for that release.
