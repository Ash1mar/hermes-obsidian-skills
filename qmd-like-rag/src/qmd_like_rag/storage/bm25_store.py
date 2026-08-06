from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class BM25Store:
    def __init__(self) -> None:
        self.docs: list[list[str]] = []
        self.meta: list[dict[str, Any]] = []
        self.bm25: Any = None

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold())

    def index_documents(self, documents: list[dict[str, Any]]) -> None:
        from rank_bm25 import BM25Okapi

        self.docs = []
        self.meta = []
        for document in documents:
            tokens = self.tokenize(str(document.get("text", "")))
            if tokens:
                self.docs.append(tokens)
                self.meta.append(dict(document))
        self.bm25 = BM25Okapi(self.docs) if self.docs else None

    def search(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        if self.bm25 is None:
            return []
        scores = self.bm25.get_scores(self.tokenize(query))
        ranking = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:top_k]
        results: list[dict[str, Any]] = []
        for index, score in ranking:
            if score <= 0:
                continue
            item = dict(self.meta[index])
            item["bm25_score"] = float(score)
            results.append(item)
        return results

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"docs": self.docs, "meta": self.meta}, ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path) -> None:
        from rank_bm25 import BM25Okapi

        if not path.exists():
            self.docs, self.meta, self.bm25 = [], [], None
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.docs = data.get("docs", [])
        self.meta = data.get("meta", [])
        self.bm25 = BM25Okapi(self.docs) if self.docs else None
