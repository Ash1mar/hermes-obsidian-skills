from __future__ import annotations

from typing import Any


def rrf_fuse(result_lists: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    scores: dict[tuple[str, int, str], float] = {}
    cache: dict[tuple[str, int, str], dict[str, Any]] = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            key = (str(item.get("source")), int(item.get("start_line") or 1), str(item.get("id") or item.get("text", "")[:80]))
            cache.setdefault(key, dict(item))
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank + 1)
    fused = []
    for key, item in cache.items():
        item["rrf_score"] = scores[key]
        fused.append(item)
    return sorted(fused, key=lambda item: float(item["rrf_score"]), reverse=True)
