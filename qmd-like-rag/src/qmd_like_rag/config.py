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
    embedding_revision: str | None = None
    reranker_revision: str | None = None
    embedding_dimension: int | None = None
    local_files_only: bool = False
    require_immutable_model_revisions: bool = False
    device: str = "cpu"
    use_reranker: bool = True
    embedding_backend: str = "sentence_transformers"
    reranker_backend: str = "cross_encoder"
    embedding_endpoint: str | None = None
    reranker_endpoint: str | None = None
    embedding_request_model: str | None = None
    embedding_api_key_env: str | None = None
    reranker_api_key_env: str | None = None
    embedding_batch_size: int = 32
    model_request_timeout_seconds: float = 120.0
    model_request_max_retries: int = 2
    model_audit_mode: str | None = None
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
        if self.device not in {"cpu", "cuda", "mps"}:
            raise ValueError("device must be one of: cpu, cuda, mps")
        if self.embedding_dimension is not None and self.embedding_dimension < 1:
            raise ValueError("embedding_dimension must be positive")
        if self.embedding_backend not in {"sentence_transformers", "openai_http"}:
            raise ValueError("embedding_backend must be one of: sentence_transformers, openai_http")
        if self.reranker_backend not in {"cross_encoder", "openai_http"}:
            raise ValueError("reranker_backend must be one of: cross_encoder, openai_http")
        if self.embedding_batch_size < 1:
            raise ValueError("embedding_batch_size must be positive")
        if self.model_request_timeout_seconds <= 0 or self.model_request_max_retries < 0:
            raise ValueError("model request timeout must be positive and retries must be non-negative")
        if self.model_audit_mode not in {None, "immutable-revision", "name-only"}:
            raise ValueError("model_audit_mode must be one of: immutable-revision, name-only")
        if self.embedding_backend == "openai_http":
            if not self.embedding_endpoint:
                raise ValueError("embedding_endpoint is required for openai_http")
            if self.embedding_dimension is None:
                raise ValueError("embedding_dimension is required for openai_http")
        if self.use_reranker and self.reranker_backend == "openai_http" and not self.reranker_endpoint:
            raise ValueError("reranker_endpoint is required for openai_http")
        immutable_audit = self.require_immutable_model_revisions or self.model_audit_mode == "immutable-revision"
        if immutable_audit:
            revision_pattern = re.compile(r"^[0-9a-f]{40}$")
            for name, revision in (
                ("embedding_revision", self.embedding_revision),
                ("reranker_revision", self.reranker_revision),
            ):
                if not revision or not revision_pattern.fullmatch(revision):
                    raise ValueError(f"{name} must be a full 40-character commit hash")
            if self.embedding_dimension is None:
                raise ValueError("embedding_dimension is required for immutable model audit")

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
        new_fields = {
            "embedding_backend",
            "reranker_backend",
            "embedding_endpoint",
            "reranker_endpoint",
            "embedding_request_model",
            "embedding_api_key_env",
            "reranker_api_key_env",
            "embedding_batch_size",
            "model_request_timeout_seconds",
            "model_request_max_retries",
            "model_audit_mode",
        }
        remote = self.embedding_backend != "sentence_transformers" or (
            self.use_reranker and self.reranker_backend != "cross_encoder"
        )
        if not remote and self.model_audit_mode is None:
            # Keep 0.3.x local-model fingerprints byte-for-byte compatible so
            # existing WSL indexes remain usable after upgrading the package.
            for name in new_fields:
                data.pop(name, None)
        else:
            embedding_endpoint = data.pop("embedding_endpoint", None)
            reranker_endpoint = data.pop("reranker_endpoint", None)
            data.pop("embedding_api_key_env", None)
            data.pop("reranker_api_key_env", None)
            if embedding_endpoint:
                data["embedding_endpoint_fingerprint"] = self._endpoint_fingerprint(embedding_endpoint)
            if reranker_endpoint:
                data["reranker_endpoint_fingerprint"] = self._endpoint_fingerprint(reranker_endpoint)
        return data

    @staticmethod
    def _endpoint_fingerprint(endpoint: str) -> str:
        return "sha256:" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

    def config_fingerprint(self) -> str:
        payload = json.dumps(self.portable_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def model_fingerprint(self) -> str:
        payload = json.dumps(self.model_manifest(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def model_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "embedding": {
                "identity": self.embedding_model,
                "revision": self.embedding_revision,
                "dimension": self.embedding_dimension,
            },
            "reranker": (
                {
                    "identity": self.reranker_model,
                    "revision": self.reranker_revision,
                }
                if self.use_reranker
                else None
            ),
            "local_files_only": self.local_files_only,
        }
        remote = self.embedding_backend != "sentence_transformers" or (
            self.use_reranker and self.reranker_backend != "cross_encoder"
        )
        if remote or self.model_audit_mode is not None:
            assurance = self.model_audit_mode or (
                "immutable-revision" if self.require_immutable_model_revisions else "name-only"
            )
            manifest["embedding"].update(
                {"backend": self.embedding_backend, "assurance": assurance}
            )
            if manifest["reranker"] is not None:
                manifest["reranker"].update(
                    {"backend": self.reranker_backend, "assurance": assurance}
                )
        return manifest


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
