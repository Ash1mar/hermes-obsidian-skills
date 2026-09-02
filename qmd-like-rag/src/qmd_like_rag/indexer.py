from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .chunker import chunk_markdown_file
from .corpus import SourceDocument, resolve_sources
from .storage.bm25_store import BM25Store
from .storage.chroma_store import ChromaStore
from .storage.fingerprint_store import FingerprintStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HybridIndexer:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.config.ensure_dirs()
        self.chroma = ChromaStore(config)
        self.bm25 = BM25Store()
        self.fingerprints = FingerprintStore(config.fingerprint_path())

    def _chunks_for(self, source: SourceDocument) -> list[dict[str, Any]]:
        source_sha256 = self.fingerprints.fingerprint(source.path)
        return chunk_markdown_file(
            source.path,
            source_id=source.vault_path,
            source_sha256=source_sha256,
            chunk_size=self.config.chunk_size,
            overlap_ratio=self.config.chunk_overlap,
        )

    def _write_state(self, *, status: str, documents: int, chunks: int, errors: list[str]) -> dict[str, Any]:
        corpus_payload = json.dumps(self.fingerprints.data, sort_keys=True).encode("utf-8")
        corpus_fingerprint = "sha256:" + hashlib.sha256(corpus_payload).hexdigest()
        index_payload = "|".join(
            [self.config.config_fingerprint(), self.config.model_fingerprint(), corpus_fingerprint, str(chunks)]
        ).encode("utf-8")
        state = {
            "schema_version": "1.0",
            "protocol_version": "hermes-coarse-recall/v1",
            "provider": "qmd-like-rag",
            "provider_version": __version__,
            "vault_id": self.config.vault_id,
            "status": status,
            "generated_at": utc_now(),
            "configuration": self.config.portable_dict(),
            "configuration_fingerprint": self.config.config_fingerprint(),
            "model_fingerprint": self.config.model_fingerprint(),
            "models": self.config.model_manifest(),
            "corpus_fingerprint": corpus_fingerprint,
            "index_fingerprint": "sha256:" + hashlib.sha256(index_payload).hexdigest(),
            "document_count": documents,
            "chunk_count": chunks,
            "errors": errors,
        }
        self.config.state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return state

    def sync(self, rebuild: bool = False) -> dict[str, Any]:
        sources = resolve_sources(self.config.vault_root, self.config.include_patterns)
        source_map = {source.vault_path: source for source in sources}
        current_ids = set(source_map)
        indexed_ids = self.fingerprints.all_files()
        removed = sorted(indexed_ids - current_ids)
        changed = sorted(
            source_id
            for source_id, source in source_map.items()
            if rebuild or self.fingerprints.has_changed(source_id, source.path)
        )
        if rebuild:
            self.chroma.reset()
            self.bm25 = BM25Store()
            self.fingerprints.data.clear()
            removed = []
        for source_id in removed:
            self.chroma.delete_source(source_id)
            self.fingerprints.remove(source_id)
        changed_chunks: list[dict[str, Any]] = []
        for source_id in changed:
            self.chroma.delete_source(source_id)
            changed_chunks.extend(self._chunks_for(source_map[source_id]))
        if changed_chunks:
            self.chroma.upsert(changed_chunks)
        if changed or removed or rebuild or not self.config.bm25_path().exists():
            all_chunks: list[dict[str, Any]] = []
            for source in sources:
                all_chunks.extend(self._chunks_for(source))
            self.bm25.index_documents(all_chunks)
            self.bm25.save(self.config.bm25_path())
        else:
            self.bm25.load(self.config.bm25_path())
        for source_id in changed:
            self.fingerprints.update(source_id, source_map[source_id].path)
        self.fingerprints.save()
        state = self._write_state(
            status="ready",
            documents=len(sources),
            chunks=self.chroma.count(),
            errors=[],
        )
        state["updated_documents"] = changed
        state["removed_documents"] = removed
        return state

    def load(self) -> None:
        self.bm25.load(self.config.bm25_path())
