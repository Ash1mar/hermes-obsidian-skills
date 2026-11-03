from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import __version__
from .corpus import current_release_identity, load_release_corpus
from .renderer import render_projections
from .storage.bm25_store import BM25Store
from .storage.chroma_store import ChromaStore
from .storage.embedding_cache import EmbeddingCache
from .sync_support import bounded_batches, fingerprint, read_json, write_atomic, writer_lock
from .tokenization import ModelTokenizer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HybridIndexer:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.chroma: ChromaStore | None = None
        self.bm25: BM25Store | None = None
        self.metrics: dict[str, Any] = {}
        self.progress: dict[str, Any] = {}

    @contextmanager
    def _timed(self, phase: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            timings = self.metrics.setdefault("seconds", {})
            timings[phase] = timings.get(phase, 0.0) + time.perf_counter() - started

    def _progress(self, phase: str, **values: Any) -> None:
        self.progress.update(phase=phase, updated_at=utc_now(), **values)
        self.progress["metrics"] = self.metrics
        write_atomic(self.config.progress_path(), self.progress)

    def _generation(self, corpus: Any, tokenizer: ModelTokenizer) -> str:
        return fingerprint({
            "release_id": corpus.release_id, "release_hash": corpus.release_hash,
            "index_configuration_fingerprint": self.config.index_config_fingerprint(),
            "tokenizer_fingerprint": tokenizer.fingerprint,
            "input_fingerprint": fingerprint(corpus.projections),
        }).removeprefix("sha256:")

    def sync(self, rebuild: bool = False, expected_release_id: str | None = None,
             expected_release_hash: str | None = None) -> dict[str, Any]:
        with writer_lock(self.config.persist_dir):
            started = time.perf_counter()
            self.started = started
            self.metrics = {"seconds": {}, "embedded_documents": 0, "cached_documents": 0,
                            "resumed_documents": 0, "deduplicated_documents": 0,
                            "completed_batches": 0}
            self.progress = {"status": "building", "started_at": utc_now(),
                             "release_id": expected_release_id, "release_hash": expected_release_hash}
            self._progress("validation")
            try:
                result = self._sync(rebuild, expected_release_id, expected_release_hash)
                self.metrics["seconds"]["total"] = time.perf_counter() - started
                self._progress("complete", status="ready")
                return result
            except Exception as exc:
                self.metrics["seconds"]["total"] = time.perf_counter() - started
                self._progress(self.progress["phase"], status="failed",
                               error=f"{type(exc).__name__}: {exc}")
                raise

    def _sync(self, rebuild: bool, expected_release_id: str | None,
              expected_release_hash: str | None) -> dict[str, Any]:
        with self._timed("validation"):
            corpus = load_release_corpus(self.config.vault_root)
            if expected_release_id and corpus.release_id != expected_release_id:
                raise RuntimeError("Current release differs from submitted release_id")
            if expected_release_hash and corpus.release_hash != expected_release_hash:
                raise RuntimeError("Current release differs from submitted release_hash")
            tokenizer = ModelTokenizer(self.config)
            reranker_tokenizer = (ModelTokenizer(self.config, "reranker")
                                  if self.config.use_reranker else None)
        self._progress("render", release_id=corpus.release_id, release_hash=corpus.release_hash)
        with self._timed("render"):
            documents = render_projections(corpus.projections, tokenizer, self.config.renderer_version)
        if len({row["id"] for row in documents}) != len(documents):
            raise RuntimeError("Release contains duplicate projection identities")
        base_generation = self._generation(corpus, tokenizer)
        existing = read_json(self.config.state_path())
        # --rebuild always creates another candidate, including retries of the same release.
        # It must never reset a generation that a reader can already have selected.
        generation = base_generation + ("-" + uuid.uuid4().hex if rebuild else "")
        input_fingerprint = fingerprint(documents)
        embedding_identity = {"index": self.config.index_configuration(),
                              "tokenizer_fingerprint": tokenizer.fingerprint}
        embedding_fingerprint = fingerprint(embedding_identity)
        query_fields = {
            "configuration": self.config.portable_dict(),
            "configuration_fingerprint": self.config.config_fingerprint(),
            "model_fingerprint": self.config.model_fingerprint(), "models": self.config.model_manifest(),
            "query_configuration_fingerprint": self.config.query_config_fingerprint(),
            "reranker_tokenizer": reranker_tokenizer.manifest() if reranker_tokenizer else None,
        }
        if (not rebuild and existing and existing.get("status") == "ready"
                and existing.get("base_generation") == base_generation):
            selected = self.config.with_generation(str(existing["index_generation"]))
            manifest = read_json(selected.generation_dir / "projection-manifest.json")
            if (not manifest or manifest.get("input_fingerprint") != input_fingerprint
                    or not selected.bm25_path().is_file() or not selected.chroma_path().is_dir()):
                raise RuntimeError("Ready generation is incomplete; use an authorized --rebuild")
            if current_release_identity(self.config.vault_root) != (corpus.release_id, corpus.release_hash):
                raise RuntimeError("Current release changed before sync completion")
            self.metrics["seconds"]["total"] = time.perf_counter() - self.started
            state = {**existing, **query_fields, "sync_metrics": self.metrics}
            write_atomic(self.config.state_path(), state)
            self._progress("complete", index_generation=state["index_generation"],
                           completed_documents=len(documents), total_documents=len(documents))
            return {**state, "updated_documents": [], "removed_documents": []}
        # A failed deterministic candidate can be resumed. A selected generation is immutable.
        if existing and existing.get("index_generation") == generation:
            raise RuntimeError("Refusing to replace a selected generation; use --rebuild")
        generation_config = self.config.with_generation(generation)
        generation_config.ensure_dirs()
        checkpoint_path = generation_config.generation_dir / "build-state.json"
        checkpoint = read_json(checkpoint_path)
        if checkpoint and checkpoint.get("input_fingerprint") != input_fingerprint:
            raise RuntimeError("Candidate input identity changed; use --rebuild")
        checkpoint = checkpoint or {"input_fingerprint": input_fingerprint, "batches": {}}
        self._progress("model_init", index_generation=generation, total_documents=len(documents),
                       completed_documents=0)
        with self._timed("model_init"):
            self.chroma = ChromaStore(generation_config)
        # Cache reuse requires an immutable embedding revision; a name-only service can change
        # behind its URL. Such services still benefit from verified batch recovery.
        cache_enabled = bool(self.config.embedding_cache_enabled
                             and re.fullmatch(r"[0-9a-f]{40}", self.config.embedding_revision or "")
                             and (self.config.embedding_backend != "openai_http"
                                  or (self.config.model_audit_mode or (
                                      "immutable-revision" if self.config.require_immutable_model_revisions
                                      else "name-only")) == "immutable-revision"))
        cache_namespace = fingerprint({"embedding": embedding_fingerprint,
                                       "dimension": self.chroma.embedding_dimension})
        cache = (EmbeddingCache(self.config.persist_dir / "embedding-cache.sqlite3", cache_namespace,
                                self.chroma.embedding_dimension) if cache_enabled else None)
        self.metrics["embedding_cache_enabled"] = cache_enabled
        max_rows = min(self.config.index_batch_size, self.chroma.max_batch_size())
        completed = 0
        try:
            for batch in bounded_batches(documents, max_rows, self.config.embedding_batch_max_tokens):
                batch_id = fingerprint(batch)
                saved = checkpoint["batches"].get(batch_id)
                with self._timed("batch_verification"):
                    digest = self.chroma.batch_digest(batch) if saved else None
                if saved and digest is not None and saved == digest:
                    self.metrics["resumed_documents"] += len(batch)
                else:
                    self._progress("embedding", completed_documents=completed)
                    with self._timed("embedding"):
                        vectors = self._vectors(batch, cache)
                    self._progress("vector_write")
                    with self._timed("vector_write"):
                        self.chroma.upsert(batch, vectors)
                    with self._timed("batch_verification"):
                        digest = self.chroma.batch_digest(batch)
                        if digest is None:
                            raise RuntimeError("Written vector batch failed read-back verification")
                    checkpoint["batches"][batch_id] = digest
                    write_atomic(checkpoint_path, checkpoint)
                completed += len(batch)
                self.metrics["completed_batches"] += 1
                self._progress("vector_write", completed_documents=completed)
            if self.chroma.count() != len(documents):
                raise RuntimeError("Candidate vector count does not match the release")
        finally:
            if cache:
                cache.close()
        self._progress("bm25")
        with self._timed("bm25"):
            self.bm25 = BM25Store()
            self.bm25.index_documents(documents)
            self.bm25.save(generation_config.bm25_path())
        self._progress("final_validation")
        with self._timed("final_validation"):
            # Revalidate hashes, permissions and the release after the potentially long build.
            verified = load_release_corpus(self.config.vault_root)
            if (verified.release_id != corpus.release_id or verified.release_hash != corpus.release_hash
                    or fingerprint(verified.projections) != fingerprint(corpus.projections)):
                raise RuntimeError("Release inputs changed during sync; candidate was not published")
        projection_manifest = {
            "schema_version": "2.0", "release_id": corpus.release_id,
            "release_hash": corpus.release_hash, "index_generation": generation,
            "input_fingerprint": input_fingerprint, "embedding_fingerprint": embedding_fingerprint,
            "index_configuration_fingerprint": self.config.index_config_fingerprint(),
            "renderer_version": self.config.renderer_version,
            "renderer_fingerprint": fingerprint({"version": self.config.renderer_version}),
            "tokenizer": tokenizer.manifest(), **query_fields,
            "projections": [{key: row.get(key) for key in (
                "id", "projection_kind", "projection_fingerprint", "unit_ref", "page_id",
                "page_revision_id", "source_unit_refs", "source", "source_sha256", "token_count",
                "subspan", "subspan_reason", "projection_content_sha256")} for row in documents],
        }
        write_atomic(generation_config.generation_dir / "projection-manifest.json", projection_manifest)
        self.metrics["seconds"]["total"] = time.perf_counter() - self.started
        state = {
            "schema_version": "2.0", "protocol_version": "hermes-coarse-recall/v1",
            "index_contract": "release-index/v2",
            "provider": "qmd-like-rag", "provider_version": __version__,
            "authority": "candidate-navigation-only", "vault_id": self.config.vault_id,
            "status": "ready", "generated_at": utc_now(),
            "capabilities": {"source_units": True, "release_driven": True,
                             "projection_kinds": ["source_unit", "knowledge_page"]},
            "release_id": corpus.release_id, "release_hash": corpus.release_hash,
            "index_generation": generation, "base_generation": base_generation,
            "index_configuration_fingerprint": self.config.index_config_fingerprint(),
            "embedding_fingerprint": embedding_fingerprint,
            "renderer_version": self.config.renderer_version,
            "renderer_fingerprint": projection_manifest["renderer_fingerprint"],
            "tokenizer": tokenizer.manifest(), **query_fields,
            "corpus_fingerprint": fingerprint({"release_hash": corpus.release_hash,
                                                "projections": input_fingerprint}),
            "index_fingerprint": fingerprint({"generation": generation, "count": len(documents)}),
            "document_count": len(documents), "chunk_count": len(documents),
            "projection_counts": {kind: sum(row["projection_kind"] == kind for row in documents)
                                  for kind in ("source_unit", "knowledge_page")},
            "sync_metrics": self.metrics, "errors": [],
        }
        write_atomic(self.config.state_path(), state)
        return {**state, "updated_documents": [row["id"] for row in documents],
                "removed_documents": []}

    def _vectors(self, batch: list[dict[str, Any]], cache: EmbeddingCache | None) -> list[list[float]]:
        texts = [row["text"] for row in batch]
        by_text = {text: cache.get(text) if cache else None for text in dict.fromkeys(texts)}
        missing = [text for text, vector in by_text.items() if vector is None]
        if missing:
            embedded = self.chroma.embed_texts(missing)
            if cache:
                cache.put(missing, embedded)
            by_text.update(zip(missing, embedded))
        self.metrics["embedded_documents"] += len(missing)
        self.metrics["cached_documents"] += len(by_text) - len(missing)
        self.metrics["deduplicated_documents"] += len(texts) - len(by_text)
        return [by_text[text] for text in texts]

    def load(self, generation: str | None = None) -> None:
        if generation is None:
            state = json.loads(self.config.state_path().read_text(encoding="utf-8"))
            generation = str(state["index_generation"])
        generation_config = self.config.with_generation(generation)
        self.chroma = ChromaStore(generation_config, read_only=True)
        self.bm25 = BM25Store()
        if not generation_config.bm25_path().is_file():
            raise RuntimeError("Ready BM25 generation is missing")
        self.bm25.load(generation_config.bm25_path())
