from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_INCLUDE_PATTERNS = [
    "30_Cards/**/*.md",
    "40_Concepts/**/*.md",
    "50_Projects/**/*.md",
    "_system/reports/*.source-map.md",
    "_system/reports/*.spec-index.md",
    "10_Raw/converted/**/document.md",
]


def default_state_root() -> Path:
    configured = os.environ.get("QMD_LIKE_RAG_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "qmd-like-rag"


def stable_vault_id(vault_root: Path) -> str:
    resolved = vault_root.resolve()
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", resolved.name).strip("-") or "vault"
    digest = hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()[:12]
    return f"{stem}-{digest}".lower()


@dataclass
class ProviderConfig:
    vault_root: Path
    state_root: Path = field(default_factory=default_state_root)
    vault_id: str = ""
    include_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_PATTERNS))
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-large"
    use_reranker: bool = True
    chunk_size: int = 800
    chunk_overlap: float = 0.15
    top_k: int = 20
    rerank_top_k: int = 12
    rrf_k: int = 60
    dedup_similarity_threshold: float = 0.7
    max_same_parent: int = 1
    ignore_chunk_types: list[str] = field(default_factory=lambda: ["navigation", "backlink"])

    def __post_init__(self) -> None:
        self.vault_root = Path(self.vault_root).expanduser().resolve()
        self.state_root = Path(self.state_root).expanduser().resolve()
        if not self.vault_id:
            self.vault_id = stable_vault_id(self.vault_root)
        if not 0 <= self.chunk_overlap < 1:
            raise ValueError("chunk_overlap must be in [0, 1)")
        if self.chunk_size < 1 or self.top_k < 1 or self.rerank_top_k < 1:
            raise ValueError("chunk_size and retrieval limits must be positive")

    @property
    def persist_dir(self) -> Path:
        return self.state_root / self.vault_id

    @property
    def collection_name(self) -> str:
        value = re.sub(r"[^a-zA-Z0-9_-]+", "-", self.vault_id).strip("-")
        # Chroma collection names must contain at least three characters.
        if len(value) < 3:
            value = f"vault-{value or 'default'}"
        return value[:63]

    def chroma_path(self) -> Path:
        return self.persist_dir / "chroma"

    def bm25_path(self) -> Path:
        return self.persist_dir / "bm25.json"

    def fingerprint_path(self) -> Path:
        return self.persist_dir / "fingerprints.json"

    def state_path(self) -> Path:
        return self.persist_dir / "index-state.json"

    def ensure_dirs(self) -> None:
        self.persist_dir.mkdir(parents=True, exist_ok=True)

    def portable_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("vault_root", None)
        data.pop("state_root", None)
        return data

    def config_fingerprint(self) -> str:
        payload = json.dumps(self.portable_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def model_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "embedding_model": self.embedding_model,
                "reranker_model": self.reranker_model if self.use_reranker else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return "identity-sha256:" + hashlib.sha256(payload).hexdigest()


def load_config(
    vault_root: Path,
    *,
    config_path: Path | None = None,
    state_root: Path | None = None,
    vault_id: str | None = None,
) -> ProviderConfig:
    values: dict[str, Any] = {}
    if config_path:
        loaded = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Provider config must be a JSON object")
        values.update(loaded)
    values["vault_root"] = vault_root
    if state_root is not None:
        values["state_root"] = state_root
    if vault_id:
        values["vault_id"] = vault_id
    return ProviderConfig(**values)
