from __future__ import annotations

import hashlib
import re
from typing import Any

from .fusion import rrf_fuse


class HybridRetriever:
    def __init__(self, config: Any, chroma: Any, bm25: Any) -> None:
        self.config = config
        self.chroma = chroma
        self.bm25 = bm25

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[\w\u3400-\u9fff]+", text.casefold()))

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        left_terms, right_terms = cls._terms(left), cls._terms(right)
        if not left_terms or not right_terms:
            return 0.0
        return len(left_terms & right_terms) / len(left_terms | right_terms)

    def _deduplicate(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        exact: set[str] = set()
        for item in results:
            if str(item.get("chunk_type", "normal")) in self.config.ignore_chunk_types:
                continue
            text = str(item.get("text", ""))
            if not text:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in exact:
                continue
            exact.add(digest)
            if any(self._similarity(text, str(old.get("text", ""))) >= self.config.dedup_similarity_threshold for old in output):
                continue
            output.append(item)
        return output

    def _expand_parent(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        parent_counts: dict[str, int] = {}
        for item in results:
            parent_id = str(item.get("parent_id") or item.get("id") or "")
            if parent_counts.get(parent_id, 0) >= self.config.max_same_parent:
                continue
            parent_counts[parent_id] = parent_counts.get(parent_id, 0) + 1
            expanded = dict(item)
            expanded["context"] = item.get("parent_text", item.get("text", ""))
            expanded.setdefault("score_type", "rrf")
            output.append(expanded)
        return output

    def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        candidate_limit = max(self.config.top_k, top_k or 0)
        vector = self.chroma.search(query, candidate_limit)
        lexical = self.bm25.search(query, candidate_limit)
        fused = rrf_fuse([vector, lexical], self.config.rrf_k)
        expanded = self._expand_parent(self._deduplicate(fused))
        return expanded[: max(1, top_k or self.config.rerank_top_k)]
