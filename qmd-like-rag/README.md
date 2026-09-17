# qmd-like-rag 0.5

`qmd-like-rag` is the independently deployed coarse-recall Provider for Hermes. Version 0.5 is
release-driven: it reads the current P4/P5 knowledge release, validates eligible canonical
SourceUnits through the embedded `hermes_source_units` runtime, renders one ordinary index
document per Unit, and separately projects eligible knowledge-page revisions. It no longer scans
Markdown globs, owns chunk boundaries, expands `parent_text`, or uses `tiktoken`.

The stable wire protocol remains `hermes-coarse-recall/v1` with
`authority: candidate-navigation-only`. P5 responses additionally require `source_units` and
`release_driven` capabilities, the exact release ID/hash, index generation, projection kind and
fingerprint, and a complete UnitRef for source projections. Query validates these fields and reads
the canonical Unit again before using it as evidence.

## Index lifecycle

1. Controlled ingest publishes immutable artifacts and UnitSets.
2. Knowledge Build and Vault Finalize publish a completed release with index eligibility.
3. `sync_release_index.py` submits the current release ID and manifest hash.
4. The Provider validates the release and Unit repositories, renders the corpus, measures every
   rendered input with the configured embedding-model tokenizer, then creates a new generation.
5. Chroma, BM25 and `projection-manifest.json` are stored under
   `<state-root>/<vault-id>/generations/<generation>/`; `index-state.json` selects the ready
   generation.
6. Recall refuses configuration, model or release mismatches. It never rebuilds during Query.

The first 0.5 sync is a clean rebuild. Old heading/token chunks and their indexes are not migrated.

## Tokenizer contract

Install the pinned `tokenizers` runtime and provide the model publisher's `tokenizer.json` through
`embedding_tokenizer_path`. Configure `embedding_tokenizer_id`, immutable revision,
`embedding_max_tokens`, and preferably the asset SHA-256. Sync computes and records the actual
checksum and fingerprint even when the example checksum is initially null. A missing, unreadable
or mismatched asset fails sync; there is no `cl100k_base`, character-count or alternate-tokenizer
fallback. When reranking is enabled, configure its publisher tokenizer asset, identity, revision,
maximum input and preferably checksum separately; sync and recall verify its fingerprint as well.

## Commands

```bash
qmd-like-rag doctor
qmd-like-rag status --vault-root <vault> --config <provider.json>
qmd-like-rag sync --vault-root <vault> --config <provider.json> \
  --release-id <release-id> --release-hash <sha256>
qmd-like-rag recall --vault-root <vault> --config <provider.json> \
  --query "<query>" --top-k 20
qmd-like-rag serve --vault-root <vault> --config <provider.json> --host 127.0.0.1 --port 8781
```

`doctor` reports package and SourceUnit capability. `status` reports embedding/reranker tokenizer readiness,
release/generation identity and projection counts without loading Chroma when the index is absent.
CLI and HTTP use the same runtime and response contract.

## Deployment

On `main`, install with the `local-models` extra in the independent WSL virtual environment and
keep model/tokenizer assets and state on the WSL-native filesystem. On `intranet`, deploy the same
tag as a CPU container that calls approved remote embedding/reranker endpoints. Keep the Vault
read-only in the Provider container and use a directly inspectable host bind such as
`${HERMES_HOST_DATA_ROOT}/phq/qmd-like-rag-state` for index state. A Docker named volume is suitable
for development but is not required by the architecture.

Build the intranet image in an approved connected environment with pinned Python packages,
tokenizer assets and checksums, record the image digest, then import it. Build inside the intranet
only when internal package, image, asset and scanning infrastructure exists. Endpoints and secrets
remain host configuration and never enter Vault manifests.

The checked-in intranet Compose example uses image `qmd-like-rag:0.5.0-intranet`, external network
`hermes-runtime`, read-only Vault `/opt/data/phq/testVault`, and host-bound state
`${HERMES_HOST_DATA_ROOT}/phq/qmd-like-rag-state`. On the current target host,
`HERMES_HOST_DATA_ROOT` is `/data/data/hermes_agent0/.hermes`. Keep both the Query and Finalize
adapters disabled until `/health`, initial release sync, `/status`, tokenizer fingerprints and the
Vault retrieval manifest agree.

The Vault is the control plane; Provider generations are rebuildable data. Deleting a Provider
generation never deletes SourceUnits, knowledge pages, release history or evidence.
