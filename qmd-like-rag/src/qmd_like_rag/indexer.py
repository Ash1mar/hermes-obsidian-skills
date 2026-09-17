from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .corpus import load_release_corpus
from .renderer import render_projections
from .storage.bm25_store import BM25Store
from .storage.chroma_store import ChromaStore
from .tokenization import ModelTokenizer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class HybridIndexer:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.chroma: ChromaStore | None = None
        self.bm25: BM25Store | None = None

    def _generation(self, corpus: Any, tokenizer: ModelTokenizer,
                    reranker_tokenizer: ModelTokenizer | None) -> str:
        return fingerprint({
            "release_id": corpus.release_id, "release_hash": corpus.release_hash,
            "renderer_version": self.config.renderer_version,
            "tokenizer_fingerprint": tokenizer.fingerprint,
            "reranker_tokenizer_fingerprint": (reranker_tokenizer.fingerprint
                                                 if reranker_tokenizer else None),
            "model_fingerprint": self.config.model_fingerprint(),
        }).removeprefix("sha256:")

    def sync(self, rebuild: bool = False, expected_release_id: str | None = None,
             expected_release_hash: str | None = None) -> dict[str, Any]:
        corpus = load_release_corpus(self.config.vault_root)
        if expected_release_id and corpus.release_id != expected_release_id:
            raise RuntimeError("Current release differs from submitted release_id")
        if expected_release_hash and corpus.release_hash != expected_release_hash:
            raise RuntimeError("Current release differs from submitted release_hash")
        tokenizer = ModelTokenizer(self.config)
        reranker_tokenizer = (ModelTokenizer(self.config, "reranker")
                              if self.config.use_reranker else None)
        documents = render_projections(corpus.projections, tokenizer, self.config.renderer_version)
        generation = self._generation(corpus, tokenizer, reranker_tokenizer)
        existing = None
        if self.config.state_path().is_file():
            existing = json.loads(self.config.state_path().read_text(encoding="utf-8"))
        if not rebuild and existing and existing.get("status") == "ready" and existing.get("index_generation") == generation:
            return {**existing, "updated_documents": [], "removed_documents": []}
        generation_config = self.config.with_generation(generation)
        generation_config.ensure_dirs()
        self.chroma = ChromaStore(generation_config)
        self.chroma.reset()
        self.chroma.upsert(documents)
        self.bm25 = BM25Store()
        self.bm25.index_documents(documents)
        self.bm25.save(generation_config.bm25_path())
        projection_manifest = {
            "schema_version": "2.0", "release_id": corpus.release_id,
            "release_hash": corpus.release_hash, "index_generation": generation,
            "renderer_version": self.config.renderer_version,
            "renderer_fingerprint": fingerprint({"version": self.config.renderer_version}),
            "tokenizer": tokenizer.manifest(), "model_fingerprint": self.config.model_fingerprint(),
            "reranker_tokenizer": (reranker_tokenizer.manifest() if reranker_tokenizer else None),
            "projections": [{key: row.get(key) for key in (
                "id", "projection_kind", "projection_fingerprint", "unit_ref", "page_id",
                "page_revision_id", "source_unit_refs", "source", "source_sha256", "token_count")}
                | {"subspan": row.get("subspan")}
                | {"subspan_reason": row.get("subspan_reason"),
                   "projection_content_sha256": row.get("projection_content_sha256")}
                for row in documents],
        }
        _write_atomic(generation_config.generation_dir / "projection-manifest.json", projection_manifest)
        counts = {kind: sum(row["projection_kind"] == kind for row in documents)
                  for kind in ("source_unit", "knowledge_page")}
        state = {
            "schema_version": "2.0", "protocol_version": "hermes-coarse-recall/v1",
            "provider": "qmd-like-rag", "provider_version": __version__,
            "authority": "candidate-navigation-only", "vault_id": self.config.vault_id,
            "status": "ready", "generated_at": utc_now(),
            "capabilities": {"source_units": True, "release_driven": True,
                             "projection_kinds": ["source_unit", "knowledge_page"]},
            "release_id": corpus.release_id, "release_hash": corpus.release_hash,
            "index_generation": generation,
            "configuration": self.config.portable_dict(),
            "configuration_fingerprint": self.config.config_fingerprint(),
            "renderer_version": self.config.renderer_version,
            "renderer_fingerprint": projection_manifest["renderer_fingerprint"],
            "tokenizer": tokenizer.manifest(),
            "reranker_tokenizer": (reranker_tokenizer.manifest() if reranker_tokenizer else None),
            "model_fingerprint": self.config.model_fingerprint(), "models": self.config.model_manifest(),
            "corpus_fingerprint": fingerprint({"release_hash": corpus.release_hash,
                                                "projections": [row["projection_fingerprint"] for row in documents]}),
            "index_fingerprint": fingerprint({"generation": generation, "count": len(documents)}),
            "document_count": len(documents), "chunk_count": len(documents),
            "projection_counts": counts, "errors": [],
        }
        _write_atomic(self.config.state_path(), state)
        return {**state, "updated_documents": [row["id"] for row in documents],
                "removed_documents": []}

    def load(self, generation: str | None = None) -> None:
        if generation is None:
            state = json.loads(self.config.state_path().read_text(encoding="utf-8"))
            generation = str(state["index_generation"])
        generation_config = self.config.with_generation(generation)
        self.chroma = ChromaStore(generation_config)
        self.bm25 = BM25Store()
        self.bm25.load(generation_config.bm25_path())
