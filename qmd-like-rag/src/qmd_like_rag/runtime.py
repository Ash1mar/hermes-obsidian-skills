from __future__ import annotations

import importlib.util
import json
from typing import Any

from . import __version__
from .config import ProviderConfig
from .contract import PROTOCOL_VERSION, PROVIDER_ID, normalize_candidate, recall_response


def read_status(config: ProviderConfig) -> dict[str, Any]:
    if not config.state_path().is_file():
        return {
            "schema_version": "1.0",
            "protocol_version": PROTOCOL_VERSION,
            "provider": PROVIDER_ID,
            "provider_version": __version__,
            "vault_id": config.vault_id,
            "status": "absent",
            "configuration": config.portable_dict(),
            "configuration_fingerprint": config.config_fingerprint(),
            "model_fingerprint": config.model_fingerprint(),
            "models": config.model_manifest(),
            "index_fingerprint": None,
            "document_count": 0,
            "chunk_count": 0,
            "errors": [],
        }
    return json.loads(config.state_path().read_text(encoding="utf-8"))


def sync(config: ProviderConfig, rebuild: bool = False) -> dict[str, Any]:
    from .indexer import HybridIndexer

    return HybridIndexer(config).sync(rebuild=rebuild)


def recall(config: ProviderConfig, query: str, top_k: int | None = None) -> dict[str, Any]:
    state = read_status(config)
    if state.get("status") != "ready":
        return recall_response(
            vault_id=config.vault_id,
            index_fingerprint=state.get("index_fingerprint"),
            candidates=[],
            warnings=["index-not-ready"],
        )
    compatibility_warnings: list[str] = []
    if state.get("configuration_fingerprint") != config.config_fingerprint():
        compatibility_warnings.append("index-configuration-mismatch")
    if state.get("model_fingerprint") != config.model_fingerprint():
        compatibility_warnings.append("index-model-mismatch")
    if compatibility_warnings:
        return recall_response(
            vault_id=config.vault_id,
            index_fingerprint=state.get("index_fingerprint"),
            candidates=[],
            warnings=compatibility_warnings,
        )
    from .indexer import HybridIndexer
    from .reranker import configured_reranker
    from .retriever import HybridRetriever

    indexer = HybridIndexer(config)
    indexer.load()
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
    )


def doctor() -> dict[str, Any]:
    packages = {
        name: importlib.util.find_spec(name) is not None
        for name in ("chromadb", "rank_bm25", "sentence_transformers", "tiktoken")
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
        "status": "ok" if all(packages[name] for name in ("chromadb", "rank_bm25", "tiktoken")) else "unavailable",
        "packages": packages,
        "optional_local_models_available": packages["sentence_transformers"],
        "torch": torch_runtime,
    }
