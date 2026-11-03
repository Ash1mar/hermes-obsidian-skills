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
6. Recall refuses indexing-configuration, embedding/tokenizer or release mismatches. Query-only
   settings and the current verified reranker can change without rebuilding vectors. Each response
   records the effective query configuration/model fingerprints. It never rebuilds during Query.

The first 0.5 sync is a clean rebuild. Old heading/token chunks and their indexes are not migrated.

### Bounded sync and recovery

Sync has one nonblocking file lock per Vault, shared by CLI and HTTP processes. A concurrent
writer fails promptly. HTTP status remains readable during sync; retrieval selects an immutable
ready generation. No partially built candidate becomes query-ready.

`index_batch_size` (default 128) and `embedding_batch_max_tokens` (32768) bound each vector
window, also respecting Chroma's write limit. `embedding_batch_size` (32) bounds individual
model calls inside that window. An input larger than the batch token budget fails explicitly;
increase the budget rather than truncating it. The corpus and BM25 are still built in memory;
this change bounds vector generation/write memory, not the entire ingestion process.

After each window, sync reads back text, complete index provenance and finite vectors of the
expected dimension, then saves a checksummed batch completion record. Retrying the same sync
revalidates current release inputs and saved batches and only writes missing or invalid batches.
Changing batch limits does not invalidate vectors. Changed source identities or permissions
are never bypassed by a checkpoint. BM25 remains a full build for the selected release.

`embedding-cache.sqlite3` stores rebuildable vectors on the Provider host, outside the Vault.
Its key includes the exact rendered text, embedding identity/revision, effective tokenizer,
preprocessing, endpoint/request model and vector dimension. Each vector has a checksum.
Cross-release reuse requires a full immutable embedding revision; remote `name-only` assurance
disables it even if a revision label was supplied. `embedding_cache_enabled: false` disables
this cache. Text deduplication and valid batch recovery still apply without the cache.

`sync-progress.json` reports phases, counts and timings without selecting the candidate.
`status.sync_progress.writer_active` distinguishes live work from an interrupted process;
reading status never repairs or resumes it. `sync_metrics.seconds` separates validation,
rendering, model initialization, embedding/cache lookup, vector writes, batch verification,
BM25, final validation and total time. Cache, deduplication and resumed counts are separate.
The Finalize adapter includes these portable metrics in its Vault status record.

Once all batches and BM25 are complete, sync revalidates the current corpus, hashes and access
before atomically publishing `index-state.json`. Failures preserve the selected generation.
`--rebuild` explicitly creates a fresh candidate, including for the same release, and never
resets a selected generation. It starts a fresh attempt rather than resuming a failed rebuild.
An ordinary retry resumes its deterministic candidate. Host-local generations may be removed
only when no selected state or active reader needs them; sync performs no automatic garbage
collection. Legacy generations retain their original compatibility checks until explicit sync.

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

The Vault is the control plane; Provider generations are rebuildable data. Deleting a Provider
generation never deletes SourceUnits, knowledge pages, release history or evidence.
