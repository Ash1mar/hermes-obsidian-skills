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

On `main`, install the package in the Hermes WSL environment and use the local command transport. The Vault remains under `/mnt/c/...`; provider state remains on the WSL-native filesystem. The deployed layout is:

```text
source       /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag
environment  /root/.venvs/qmd-like-rag
command      /usr/local/bin/qmd-like-rag
state root   /root/.local/state/qmd-like-rag
vault state  /root/.local/state/qmd-like-rag/<vault-id>/
host config  /root/.config/qmd-like-rag/main.json
```

`/usr/local/bin/qmd-like-rag` is only a stable symlink to the virtual environment's console command; it is not another installation. The virtual environment contains replaceable software. The state root contains mutable, rebuildable indexes and must survive routine virtual-environment replacement. `config/main.example.json` is the source template for the host config.

On `intranet`, do not deploy QMD. Install this package locally when the Hermes host can run it. The Vault is `/opt/data/phq/testVault`; the default example places Provider state beside that Vault but outside it:

```text
vault       /opt/data/phq/testVault
state root  /opt/data/phq/qmd-like-rag-state
vault state /opt/data/phq/qmd-like-rag-state/<vault-id>/
```

Set the actual Linux-local sibling path through `state_root` in the deployment's Provider config, using `config/intranet.example.json` as the template. Do not put the state root below `/opt/data/phq/testVault`, and exclude it from Obsidian, Vault synchronization, and governed-content backups. If retrieval must run on another server, start the same package with `qmd-like-rag serve` and configure the Skill adapter to use HTTP. The initial HTTP `sync` route assumes that the provider process can read its configured Vault path; document-upload synchronization is a later extension.

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

The tested `main` GPU runtime is pinned in `requirements-gpu-cu130.txt`. Install that runtime into qmd-like-rag's own virtual environment before installing the Provider. Do not add qmd-like-rag to MinerU's Python environment and do not add MinerU's site-packages directory to qmd-like-rag's import path.

```bash
python3 -m venv /root/.venvs/qmd-like-rag
/root/.venvs/qmd-like-rag/bin/python -m pip install \
  -r /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag/requirements-gpu-cu130.txt
/root/.venvs/qmd-like-rag/bin/python -m pip install \
  /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag
install -d -m 0755 /root/.config/qmd-like-rag
install -m 0644 \
  /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag/config/main.example.json \
  /root/.config/qmd-like-rag/main.json
ln -s /root/.venvs/qmd-like-rag/bin/qmd-like-rag /usr/local/bin/qmd-like-rag
```

The CUDA 13.0 lock was validated on WSL with Torch `2.11.0+cu130` and an NVIDIA GeForce RTX 5070 Ti Laptop GPU. Reusing locally installed wheel payloads to avoid a second large download is an installation optimization only: copy them into the independent qmd-like-rag environment by their wheel `RECORD` manifests, then require `pip check` and a real CUDA tensor operation to pass. Never make qmd-like-rag import MinerU's environment at runtime.

Replace an existing stable link only after confirming its resolved target belongs to the prior qmd-like-rag deployment. `qmd-like-rag doctor` must report Provider `0.3.0`, `cuda_available: true`, CUDA `13.0`, and the expected GPU on the tested `main` host. The `main` host config sets `device` to `cuda`, which is passed explicitly to both the embedding model and reranker.

Pin Hugging Face models to full commit hashes in Provider configuration. Download them as a separate deployment step, then keep `local_files_only: true` during sync and recall so a running Provider cannot silently move to a newer model revision. The model audit record includes identity, immutable revision, embedding dimension, and a fingerprint derived from those values.

For a CPU-only deployment, install a CPU Torch build and set `device` to `cpu`; do not use the CUDA `main` example unchanged. A successful doctor report proves package and accelerator readiness only. It does not prove that models are downloaded or that a Vault index exists. Record `provider_version`, configuration/model fingerprints, and the indexed corpus fingerprint in the Vault index manifest only after a successful model-resolved index build.

## Removed prototype features

The standalone Ollama answer generator, interactive chat UI, prototype `SKILL.md`, hard-coded Windows data paths, duplicate build/update launchers, estimated PDF pages, and generated `__pycache__` files were intentionally not retained. Hermes remains responsible for answer generation and original-PDF evidence verification.
