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

On `intranet`, do not deploy QMD. Run this package as a CPU-only Docker service on the Hermes server. It reads the same host Vault through its own read-only bind mount and keeps indexes in a separate read-write mount:

```text
vault       /opt/data/phq/testVault
state root  /opt/data/phq/qmd-like-rag-state
vault state /opt/data/phq/qmd-like-rag-state/<vault-id>/
```

`config/intranet.example.json` uses the confirmed batch embedding endpoint and OpenAI-style rerank endpoint. The Huawei accelerator is owned by those model services; the Provider container does not need Huawei drivers, Torch, CUDA, or model files. It validates the returned embedding count, indices, finite values, and configured 1024 dimension before writing vectors.

The intranet model audit is deliberately `name-only`: the logical names and embedding dimension are recorded while revisions remain JSON `null`. This is a lower-assurance transitional mode, not a fabricated immutable revision. A model service change under the same name cannot be detected automatically; after such a change, rebuild the complete index. Endpoint URLs are represented in the portable manifest only by SHA-256 fingerprints, and API tokens are never written there.

The Provider and Hermes containers do not see each other's filesystems. They independently mount the same host Vault, and communicate over the shared Docker network using `http://qmd-like-rag:8781`. This is why colocating them on one server remains useful even though the runtime call is HTTP.

Both repository branches carry the same Provider release after `main` is merged forward into `intranet`. Keeping intranet adapters disabled until health, initial rebuild, status, and manifest checks pass remains an explicit deployment gate.

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

Bind beyond localhost only on the private Docker network and behind the intranet's network controls. The built-in service does not add application authentication.

## Intranet container deployment

Build the same source revision that will be tagged for the internal release:

```bash
cd qmd-like-rag
docker build -t qmd-like-rag:0.4.0-intranet .
```

On the target host, prepare configuration and state. UID/GID `10001` is the non-root user inside the image:

```bash
install -d -m 0750 /data/data/hermes_agent0/.hermes/qmd-like-rag/config
install -d -o 10001 -g 10001 -m 0750 /data/data/hermes_agent0/.hermes/phq/qmd-like-rag-state
install -o 10001 -g 10001 -m 0640 config/intranet.example.json \
  /data/data/hermes_agent0/.hermes/qmd-like-rag/config/intranet.json
cp deploy/intranet/.env.example deploy/intranet/.env
docker network inspect hermes-runtime >/dev/null || docker network create hermes-runtime
docker compose --env-file deploy/intranet/.env \
  -f deploy/intranet/compose.example.yml config
docker compose --env-file deploy/intranet/.env \
  -f deploy/intranet/compose.example.yml up -d
```

Open `deploy/intranet/.env` and verify `HERMES_HOST_DATA_ROOT` before either Compose command. It is currently `/data/data/hermes_agent0/.hermes`; Compose derives the Vault, Provider state, and Provider config paths below that root. The required-variable expression refuses to create a container when the root is absent. The real `.env` is ignored by Git; only `.env.example` is versioned.

The existing Hermes container must also join `hermes-runtime`. From Hermes, `qmd-like-rag` must resolve as a service name. Do not publish port 8781 on the host unless an external client actually needs it.

Validate in this order before enabling either Skill adapter:

```bash
docker compose --env-file deploy/intranet/.env \
  -f deploy/intranet/compose.example.yml exec qmd-like-rag \
  qmd-like-rag status --vault-root /opt/data/phq/testVault \
  --config /etc/qmd-like-rag/intranet.json
docker compose --env-file deploy/intranet/.env \
  -f deploy/intranet/compose.example.yml exec qmd-like-rag \
  qmd-like-rag sync --rebuild --vault-root /opt/data/phq/testVault \
  --config /etc/qmd-like-rag/intranet.json
curl -fsS http://qmd-like-rag:8781/status
```

Run the first rebuild from a container on the shared network, then enable ingest long enough to write the portable Vault manifest. Enable query only after status and manifest fingerprints agree. An image built on an internet-connected workstation can be exported with `docker save`, copied through the approved transfer path, loaded with `docker load`, and run on the intranet host; the target must have a compatible Linux container architecture and access to the internal model URLs.

## Installation

Build and install from a tagged repository revision. Do not run the heavy environment directly from a Windows-mounted virtual environment.

The tested `main` GPU runtime is pinned in `requirements-gpu-cu130.txt`. Install that runtime into qmd-like-rag's own virtual environment before installing the Provider. Do not add qmd-like-rag to MinerU's Python environment and do not add MinerU's site-packages directory to qmd-like-rag's import path.

```bash
python3 -m venv /root/.venvs/qmd-like-rag
/root/.venvs/qmd-like-rag/bin/python -m pip install \
  -r /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag/requirements-gpu-cu130.txt
/root/.venvs/qmd-like-rag/bin/python -m pip install \
  '/mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag[local-models]'
install -d -m 0755 /root/.config/qmd-like-rag
install -m 0644 \
  /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/qmd-like-rag/config/main.example.json \
  /root/.config/qmd-like-rag/main.json
ln -s /root/.venvs/qmd-like-rag/bin/qmd-like-rag /usr/local/bin/qmd-like-rag
```

The CUDA 13.0 lock was validated on WSL with Torch `2.11.0+cu130` and an NVIDIA GeForce RTX 5070 Ti Laptop GPU. Reusing locally installed wheel payloads to avoid a second large download is an installation optimization only: copy them into the independent qmd-like-rag environment by their wheel `RECORD` manifests, then require `pip check` and a real CUDA tensor operation to pass. Never make qmd-like-rag import MinerU's environment at runtime.

Replace an existing stable link only after confirming its resolved target belongs to the prior qmd-like-rag deployment. `qmd-like-rag doctor` must report the expected Provider release, `cuda_available: true`, CUDA `13.0`, and the expected GPU on the tested `main` host. The `main` host config sets `device` to `cuda`, which is passed explicitly to both the embedding model and reranker. The new backend fields are omitted from legacy local-model fingerprints, so upgrading alone does not invalidate an existing compatible WSL index.

Pin Hugging Face models to full commit hashes in Provider configuration. Download them as a separate deployment step, then keep `local_files_only: true` during sync and recall so a running Provider cannot silently move to a newer model revision. The model audit record includes identity, immutable revision, embedding dimension, and a fingerprint derived from those values.

For a CPU-only deployment, install a CPU Torch build and set `device` to `cpu`; do not use the CUDA `main` example unchanged. A successful doctor report proves package and accelerator readiness only. It does not prove that models are downloaded or that a Vault index exists. Record `provider_version`, configuration/model fingerprints, and the indexed corpus fingerprint in the Vault index manifest only after a successful model-resolved index build.

## Removed prototype features

The standalone Ollama answer generator, interactive chat UI, prototype `SKILL.md`, hard-coded Windows data paths, duplicate build/update launchers, estimated PDF pages, and generated `__pycache__` files were intentionally not retained. Hermes remains responsible for answer generation and original-PDF evidence verification.
