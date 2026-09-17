# Retrieval Index Maintenance

Use `qmd-like-rag` as the current replaceable coarse-recall Provider. Vault Finalize decides
eligibility and publishes a release; the Provider reads that release and owns only rendering,
embedding, Chroma/BM25, reranking, index generations, models and host-local runtime state.

## Operation

```bash
python3 "<finalize-skill-root>/scripts/sync_release_index.py" <vault-root>
```

The script reads `config/retrieval-provider.json` unless `--provider-config` or `HERMES_RETRIEVAL_PROVIDER_CONFIG` overrides it. `enabled: false` records the Provider as disabled and skips sync entirely. Set it to `true` only after the Provider runtime and Chroma/BM25 indexes are ready. When enabled, main invokes the locally installed `qmd-like-rag` command. The intranet branch may use a locally installed command or an explicitly configured HTTP service; do not invent a service URL.

Run sync only after a knowledge release has been committed. The adapter pins the current release
ID and hash; a concurrent or stale release fails rather than indexing a mixed corpus. Use
`--rebuild` only for explicit maintenance or an incompatible/corrupt generation. The first P5
sync always creates a fresh generation and never migrates old Markdown chunks.

Leave both Skill adapters disabled until the Provider runtime, audited models, host configuration,
and first explicit release-owned sync are ready. When enabling command transport, point
`provider_config` at the deployment's host-local qmd-like-rag configuration. Model download or
runtime installation alone is not permission to build an index, and Query never performs the first
sync.

The script writes `_system/reports/retrieval-index-manifest.json` atomically. Preserve the prior
`last_success` when a new attempt fails. The manifest contains release, generation, renderer,
tokenizer, model, corpus and index fingerprints and projection counts, never an absolute state
path or service credential. `query_ready` requires this record to be ready for the current release.

Provider failure does not invalidate the committed release. Report the warning and leave sources,
knowledge pages, ledgers and release state unchanged.
