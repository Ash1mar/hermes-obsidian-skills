from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class ModelTokenizer:
    def __init__(self, config: Any, role: str = "embedding") -> None:
        if role not in {"embedding", "reranker"}:
            raise ValueError(f"Unsupported tokenizer role: {role}")
        configured = getattr(config, f"{role}_tokenizer_path")
        if configured is None:
            raise RuntimeError(f"{role}_tokenizer_path is required")
        path = Path(configured)
        if path.is_dir():
            path = path / "tokenizer.json"
        if not path.is_file():
            raise RuntimeError(f"{role.title()} tokenizer asset is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_sha = getattr(config, f"{role}_tokenizer_sha256")
        if expected_sha and digest != expected_sha:
            raise RuntimeError(f"{role.title()} tokenizer checksum mismatch")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("Install the pinned tokenizers runtime") from exc
        self._tokenizer = Tokenizer.from_file(str(path))
        self.path = path
        self.sha256 = digest
        self.role = role
        self.identity = getattr(config, f"{role}_tokenizer_id") or getattr(config, f"{role}_model")
        self.revision = (getattr(config, f"{role}_tokenizer_revision")
                         or getattr(config, f"{role}_revision"))
        self.max_tokens = int(getattr(config, f"{role}_max_tokens"))
        payload = f"{self.identity}|{self.revision}|{self.sha256}|{self.max_tokens}"
        self.fingerprint = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=True).ids)

    def split(self, text: str, prefix: str = "") -> list[tuple[int, int]]:
        """Return exhaustive, non-overlapping codepoint spans within the model limit."""
        if not text:
            return [(0, 0)]
        if self.count(prefix + text) <= self.max_tokens:
            return [(0, len(text))]
        spans: list[tuple[int, int]] = []
        start = 0
        while start < len(text):
            low, high = start + 1, len(text)
            best = start
            while low <= high:
                middle = (low + high) // 2
                if self.count(prefix + text[start:middle]) <= self.max_tokens:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if best == start:
                raise RuntimeError("Embedding token budget cannot hold one source codepoint with context")
            spans.append((start, best))
            start = best
        return spans

    def manifest(self) -> dict[str, Any]:
        return {"role": self.role, "identity": self.identity, "revision": self.revision, "sha256": self.sha256,
                "fingerprint": self.fingerprint, "max_tokens": self.max_tokens, "ready": True}
