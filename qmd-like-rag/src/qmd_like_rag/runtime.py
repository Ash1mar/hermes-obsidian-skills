from __future__ import annotations

import importlib.util
import json
from typing import Any

from . import __version__
from .config import ProviderConfig
from .contract import PROTOCOL_VERSION, PROVIDER_ID, normalize_candidate, recall_response
from .corpus import current_release_identity
from .tokenization import ModelTokenizer


def read_status(config: ProviderConfig) -> dict[str, Any]:
    if not config.state_path().is_file():
        try:
            tokenizer = ModelTokenizer(config).manifest()
        except Exception as exc:
            tokenizer = {"identity": config.embedding_tokenizer_id or config.embedding_model,
                         "revision": config.embedding_tokenizer_revision or config.embedding_revision,
                         "sha256": config.embedding_tokenizer_sha256,
                         "max_tokens": config.embedding_max_tokens, "ready": False,
                         "error": f"{type(exc).__name__}: {exc}"}
        reranker_tokenizer = None
        if config.use_reranker:
            try:
                reranker_tokenizer = ModelTokenizer(config, "reranker").manifest()
            except Exception as exc:
                reranker_tokenizer = {
                    "role": "reranker",
                    "identity": config.reranker_tokenizer_id or config.reranker_model,
                    "revision": config.reranker_tokenizer_revision or config.reranker_revision,
                    "sha256": config.reranker_tokenizer_sha256,
                    "max_tokens": config.reranker_max_tokens,
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return {
            "schema_version": "2.0",
            "protocol_version": PROTOCOL_VERSION,
            "provider": PROVIDER_ID,
            "provider_version": __version__,
            "vault_id": config.vault_id,
            "status": "absent",
            "capabilities": {"source_units": True, "release_driven": True,
                             "projection_kinds": ["source_unit", "knowledge_page"]},
            "configuration": config.portable_dict(),
            "configuration_fingerprint": config.config_fingerprint(),
            "model_fingerprint": config.model_fingerprint(),
            "models": config.model_manifest(),
            "tokenizer": tokenizer,
            "reranker_tokenizer": reranker_tokenizer,
            "release_id": None,
            "release_hash": None,
            "index_generation": None,
            "index_fingerprint": None,
            "document_count": 0,
            "chunk_count": 0,
            "errors": [],
        }
    return json.loads(config.state_path().read_text(encoding="utf-8"))


def sync(config: ProviderConfig, rebuild: bool = False, expected_release_id: str | None = None,
         expected_release_hash: str | None = None) -> dict[str, Any]:
    from .indexer import HybridIndexer

    return HybridIndexer(config).sync(rebuild=rebuild, expected_release_id=expected_release_id,
                                      expected_release_hash=expected_release_hash)


def recall(config: ProviderConfig, query: str, top_k: int | None = None) -> dict[str, Any]:
    state = read_status(config)
    if state.get("status") != "ready":
        return recall_response(
            vault_id=config.vault_id,
            index_fingerprint=state.get("index_fingerprint"),
            candidates=[],
            warnings=["index-not-ready"],
            release_id=state.get("release_id"), release_hash=state.get("release_hash"),
            index_generation=state.get("index_generation"),
        )
    compatibility_warnings: list[str] = []
    if state.get("configuration_fingerprint") != config.config_fingerprint():
        compatibility_warnings.append("index-configuration-mismatch")
    if state.get("model_fingerprint") != config.model_fingerprint():
        compatibility_warnings.append("index-model-mismatch")
    try:
        current_tokenizer = ModelTokenizer(config).manifest()
        if state.get("tokenizer", {}).get("fingerprint") != current_tokenizer.get("fingerprint"):
            compatibility_warnings.append("index-tokenizer-mismatch")
    except Exception as exc:
        compatibility_warnings.append(f"tokenizer-unavailable:{type(exc).__name__}")
    if config.use_reranker:
        try:
            current_reranker_tokenizer = ModelTokenizer(config, "reranker").manifest()
            if (state.get("reranker_tokenizer", {}).get("fingerprint")
                    != current_reranker_tokenizer.get("fingerprint")):
                compatibility_warnings.append("index-reranker-tokenizer-mismatch")
        except Exception as exc:
            compatibility_warnings.append(f"reranker-tokenizer-unavailable:{type(exc).__name__}")
    try:
        release_id, release_hash = current_release_identity(config.vault_root)
        if state.get("release_id") != release_id or state.get("release_hash") != release_hash:
            compatibility_warnings.append("index-release-mismatch")
    except Exception as exc:
        compatibility_warnings.append(f"release-unavailable:{type(exc).__name__}")
    if compatibility_warnings:
        return recall_response(
            vault_id=config.vault_id,
            index_fingerprint=state.get("index_fingerprint"),
            candidates=[],
            warnings=compatibility_warnings,
            release_id=state.get("release_id"), release_hash=state.get("release_hash"),
            index_generation=state.get("index_generation"),
        )
    from .indexer import HybridIndexer
    from .reranker import configured_reranker
    from .retriever import HybridRetriever

    indexer = HybridIndexer(config)
    indexer.load(str(state["index_generation"]))
    limit = max(1, top_k or config.rerank_top_k)
    raw = HybridRetriever(config, indexer.chroma, indexer.bm25).search(query, top_k=max(limit, config.top_k))
    warnings: list[str] = []
    if config.use_reranker and raw:
        try:
            raw = configured_reranker(config).rerank(query, raw, limit)
        except Exception as exc:
            warnings.append(f"reranker-unavailable:{type(exc).__name__}")
            raw = raw[:limit]
    else:
        raw = raw[:limit]
    candidates = [normalize_candidate(item, config.vault_root) for item in raw]
    return recall_response(
        vault_id=config.vault_id,
        index_fingerprint=state.get("index_fingerprint"),
        candidates=candidates,
        warnings=warnings,
        release_id=state.get("release_id"), release_hash=state.get("release_hash"),
        index_generation=state.get("index_generation"),
    )


def doctor() -> dict[str, Any]:
    packages = {
        name: importlib.util.find_spec(name) is not None
        for name in ("chromadb", "rank_bm25", "sentence_transformers", "tokenizers")
    }
    torch_runtime: dict[str, Any] = {
        "version": None,
        "cuda_available": False,
        "cuda_version": None,
        "device_name": None,
    }
    if packages["sentence_transformers"]:
        try:
            import torch

            torch_runtime.update(
                {
                    "version": torch.__version__,
                    "cuda_available": torch.cuda.is_available(),
                    "cuda_version": torch.version.cuda,
                    "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                }
            )
        except Exception as exc:
            torch_runtime["error"] = f"{type(exc).__name__}: {exc}"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "provider": PROVIDER_ID,
        "provider_version": __version__,
        "status": "ok" if all(packages[name] for name in ("chromadb", "rank_bm25", "tokenizers")) else "unavailable",
        "capabilities": {"source_units": True, "release_driven": True,
                         "projection_kinds": ["source_unit", "knowledge_page"]},
        "packages": packages,
        "optional_local_models_available": packages["sentence_transformers"],
        "torch": torch_runtime,
    }
