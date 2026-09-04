from __future__ import annotations

from typing import Any

import math

from .model_http import post_json


class BgeReranker:
    def __init__(
        self,
        model_name: str,
        device: str,
        *,
        revision: str | None = None,
        local_files_only: bool = False,
    ) -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(
            model_name,
            device=device,
            revision=revision,
            local_files_only=local_files_only,
        )

    def rerank(self, query: str, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not candidates:
            return []
        pairs = [(query, str(item.get("context") or item.get("text", ""))) for item in candidates]
        scores = self.model.predict(pairs)
        ranked = []
        for item, score in zip(candidates, scores):
            candidate = dict(item)
            candidate["rerank_score"] = float(score)
            candidate["score_type"] = "cross-encoder"
            ranked.append(candidate)
        return sorted(ranked, key=lambda item: float(item["rerank_score"]), reverse=True)[:top_k]


class OpenAIHttpReranker:
    def __init__(self, config: Any) -> None:
        self.config = config

    def rerank(self, query: str, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not candidates:
            return []
        documents = [str(item.get("context") or item.get("text", "")) for item in candidates]
        response = post_json(
            self.config.reranker_endpoint,
            {
                "model": self.config.reranker_model,
                "query": query,
                "documents": documents,
            },
            timeout=self.config.model_request_timeout_seconds,
            max_retries=self.config.model_request_max_retries,
            api_key_env=self.config.reranker_api_key_env,
        )
        results = response.get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError("Reranker response requires a non-empty results array")
        ranked: list[dict[str, Any]] = []
        seen: set[int] = set()
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("index"), int):
                raise RuntimeError("Reranker result requires an integer index")
            index = result["index"]
            if index < 0 or index >= len(candidates) or index in seen:
                raise RuntimeError("Reranker response contains an invalid or duplicate index")
            seen.add(index)
            score = float(result.get("relevance_score"))
            if not math.isfinite(score):
                raise RuntimeError("Reranker response contains a non-finite score")
            candidate = dict(candidates[index])
            candidate["rerank_score"] = score
            candidate["score_type"] = "remote-cross-encoder"
            ranked.append(candidate)
        return sorted(ranked, key=lambda item: float(item["rerank_score"]), reverse=True)[:top_k]


def configured_reranker(config: Any):
    if config.reranker_backend == "openai_http":
        return OpenAIHttpReranker(config)
    return BgeReranker(
        config.reranker_model,
        config.device,
        revision=config.reranker_revision,
        local_files_only=config.local_files_only,
    )
