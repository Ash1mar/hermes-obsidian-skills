# Coarse Retrieval Provider

Use the configured coarse-recall Provider to narrow a large Vault before traditional content search. The current Provider is `qmd-like-rag`; the Skill depends only on `hermes-coarse-recall/v1`, not on Chroma, embedding models, or Provider internals.

## Pipeline

```text
question classification
-> coarse recall || hierarchical routing
-> merge and expand candidate scope
-> scoped traditional search
-> current-source and original-PDF verification
```

Use direct traditional/hierarchical lookup for exact identifiers and verbatim phrases when cheaper. Do not use Provider top-k as a completeness boundary for gap or audit questions.

## Operation

```bash
python3 "<query-skill-root>/scripts/retrieve_candidates.py" \
  <vault-root> "<query>" --top-k 30 --trace-id <trace-id>
```

The script reads `config/retrieval-provider.json` unless `--provider-config` or `HERMES_RETRIEVAL_PROVIDER_CONFIG` overrides it.

Main normally uses command transport:

```json
{
  "provider": "qmd-like-rag",
  "transport": "command",
  "command": ["qmd-like-rag"],
  "timeout_seconds": 60
}
```

The intranet branch may replace that file or point its existing `config/intranet.json` deployment policy at HTTP:

```json
{
  "provider": "qmd-like-rag",
  "transport": "http",
  "base_url": "<configured intranet provider URL>",
  "timeout_seconds": 60
}
```

Do not store credentials or machine-local state paths in Vault metadata. Do not invent an intranet URL in the Skill; deployment must supply it.

## Result boundary

Accept only `hermes-coarse-recall/v1` responses with `authority: candidate-navigation-only`. Reject absolute paths, traversal paths, missing files, and invalid line ranges. A source-hash mismatch marks the candidate as changed but does not by itself remove it; re-open and re-locate the current source.

Provider score, section ingest status, and extraction QA status use different scales. Do not add them together. Merge by Vault-relative path and overlapping ranges, retain retrieval routes, and use source verification to determine evidence quality.

Provider failure is non-blocking. Record it in the trace and continue with hierarchical and traditional retrieval. Query never mutates or rebuilds the Provider index.
