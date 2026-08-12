from __future__ import annotations

from typing import Any


class BgeReranker:
    def __init__(self, model_name: str, device: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name, device=device)

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
