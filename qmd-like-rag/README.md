# qmd-like-rag

`qmd-like-rag` is the coarse-recall provider maintained with the Hermes Obsidian Skills repository. It was derived from the `qmd-rag-v2.1` prototype and retains its useful Markdown retrieval pipeline: heading-aware parent/child chunks, Chroma semantic retrieval, BM25 lexical retrieval, reciprocal-rank fusion, deduplication, parent expansion, optional BGE reranking, and incremental file fingerprints.

It is not a Hermes Skill and it is not a second orchestration service. The controlled ingest/query Skills own workflow and evidence verification; this package owns only indexing and candidate recall.

## Runtime boundary

- Keep source code in this repository.
- Install the package into its own Linux virtual environment.
- Keep Chroma, BM25, fingerprints, models, and caches in the provider host's local state directory, outside the Vault.
- Store only portable configuration, model/config fingerprints, and index status in the Vault.
- Treat every returned chunk as navigation-only. Re-open the current Vault source before answering.

The installed console command is the Provider itself:

```bash
qmd-like-rag doctor
qmd-like-rag sync --vault-root /path/to/vault
qmd-like-rag recall --vault-root /path/to/vault --query "question"
qmd-like-rag status --vault-root /path/to/vault
```

There is deliberately no separate `hermes-retrieval` facade in the current design.

## Indexed corpus

Default patterns select governed Markdown and converted source text while excluding query traces, page evidence, binaries, and runtime state:

```text
30_Cards/**/*.md
40_Concepts/**/*.md
50_Projects/**/*.md
_system/reports/*.source-map.md
_system/reports/*.spec-index.md
10_Raw/converted/**/document.md
```

Override patterns and models with a JSON config passed through `--config`. Machine-specific state paths belong in host configuration or `QMD_LIKE_RAG_STATE_ROOT`, not in Vault configuration.

## main and intranet

On `main`, install the package in the Hermes WSL environment and use the local command transport. The Vault remains under `/mnt/c/...`; provider state remains on the WSL-native filesystem.

On `intranet`, do not deploy QMD. Install this package locally when the Hermes host can run it. If retrieval must run on another server, start the same package with `qmd-like-rag serve` and configure the Skill adapter to use HTTP. The initial HTTP `sync` route assumes that the provider process can read its configured Vault path; document-upload synchronization is a later extension.

## HTTP transport

The optional standard-library server exposes:

- `GET /health`
- `GET /status`
- `POST /retrieve`
- `POST /sync`

```bash
qmd-like-rag serve \
  --vault-root /opt/data/phq/testVault \
  --state-root /var/lib/qmd-like-rag \
  --host 127.0.0.1 \
  --port 8781
```

Bind beyond localhost only behind the intranet's authentication and network controls.

## Installation

Build and install from a tagged repository revision. Do not run the heavy environment directly from a Windows-mounted virtual environment.

```bash
python3 -m venv /root/.venvs/qmd-like-rag
/root/.venvs/qmd-like-rag/bin/python -m pip install ./qmd-like-rag
```

Create a stable wrapper or symlink for the console command according to the deployment policy. Record `provider_version`, configuration/model fingerprints, and the indexed corpus fingerprint in the Vault index manifest.

## Removed prototype features

The standalone Ollama answer generator, interactive chat UI, prototype `SKILL.md`, hard-coded Windows data paths, duplicate build/update launchers, estimated PDF pages, and generated `__pycache__` files were intentionally not retained. Hermes remains responsible for answer generation and original-PDF evidence verification.
