# Retrieval Index Maintenance

Use `qmd-like-rag` as the current implementation of the replaceable coarse-recall Provider. Keep Provider internals outside the Skill: the Skill decides corpus timing and records status; the Provider owns chunking, embedding, Chroma/BM25, reranking, models, and runtime state.

## Operation

```bash
python3 "<ingest-skill-root>/scripts/sync_retrieval_index.py" <vault-root>
```

The script reads `config/retrieval-provider.json` unless `--provider-config` or `HERMES_RETRIEVAL_PROVIDER_CONFIG` overrides it. Main normally invokes the locally installed `qmd-like-rag` command. The intranet branch may use a locally installed command or an explicitly configured HTTP service; do not invent a service URL.

Run incremental sync after completed source ingest or at a related batch boundary. Do not sync after every intermediate section write. Use `--rebuild` only for explicit maintenance or an incompatible/corrupt index.

The script writes `_system/reports/retrieval-index-manifest.json` atomically. Preserve the prior `last_success` when a new attempt fails. The manifest contains portable protocol/provider/model/config/corpus/index fingerprints and counts, never an absolute state path or service credential.

Provider failure does not invalidate ingest. Report the warning and leave the controlled source and ledger states unchanged.
