from __future__ import annotations

import json
from typing import Any

from .embedding_cache import valid_vector
from ..sync_support import fingerprint


class ChromaStore:
    def __init__(self, config: Any, *, read_only: bool = False) -> None:
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise RuntimeError("Install qmd-like-rag runtime dependencies before using Chroma") from exc

        self.config = config
        self.path = config.chroma_path()
        if read_only and not self.path.is_dir():
            raise RuntimeError("Ready Chroma generation is missing")
        if not read_only:
            self.path.mkdir(parents=True, exist_ok=True)
        self.remote_embeddings = config.embedding_backend == "openai_http"
        if self.remote_embeddings:
            from ..embeddings import OpenAIHttpEmbeddingBackend

            self.embedder = OpenAIHttpEmbeddingBackend(config)
            self.embed_fn = None
            self.embedding_dimension = self.embedder.embedding_dimension
        else:
            from chromadb.utils import embedding_functions

            self.embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=config.embedding_model,
                device=config.device,
                revision=config.embedding_revision,
                local_files_only=config.local_files_only,
            )
            self.embedder = None
            self.embedding_dimension = int(self.embed_fn._model.get_sentence_embedding_dimension())
            if config.embedding_dimension is not None and self.embedding_dimension != config.embedding_dimension:
                raise RuntimeError(
                    "Embedding dimension mismatch: "
                    f"expected {config.embedding_dimension}, got {self.embedding_dimension}"
                )
        self.client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        self.collection = (self.client.get_collection(name=config.collection_name,
                                                     embedding_function=self.embed_fn)
                           if read_only else self._collection())

    def _collection(self):
        return self.client.get_or_create_collection(
            name=self.config.collection_name,
            embedding_function=self.embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self) -> None:
        try:
            self.client.delete_collection(self.config.collection_name)
        except Exception:
            pass
        self.collection = self._collection()

    @staticmethod
    def metadata(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "source": str(chunk["source"]),
                "source_sha256": str(chunk["source_sha256"]),
                "start_line": int(chunk["start_line"]),
                "end_line": int(chunk["end_line"]),
                "heading": str(chunk.get("heading", "")),
                "chunk_type": str(chunk.get("chunk_type", "normal")),
                "projection_kind": str(chunk["projection_kind"]),
                "projection_fingerprint": str(chunk["projection_fingerprint"]),
                "release_id": str(chunk["release_id"]),
                "release_hash": str(chunk["release_hash"]),
                "unit_ref_json": json.dumps(chunk.get("unit_ref"), sort_keys=True),
                "page_id": str(chunk.get("page_id") or ""),
                "page_revision_id": str(chunk.get("page_revision_id") or ""),
                "source_unit_refs_json": json.dumps(chunk.get("source_unit_refs", []), sort_keys=True),
                "subspan_json": json.dumps(chunk.get("subspan"), sort_keys=True),
                "subspan_reason": str(chunk.get("subspan_reason") or ""),
                "projection_content_sha256": str(chunk.get("projection_content_sha256") or ""),
            }
            for chunk in chunks
        ]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), self.config.embedding_batch_size):
            batch = texts[start:start + self.config.embedding_batch_size]
            result = self.embedder.embed(batch) if self.remote_embeddings else self.embed_fn(batch)
            if len(result) != len(batch):
                raise RuntimeError("Embedding response count mismatch")
            vectors.extend(valid_vector(list(vector), self.embedding_dimension) for vector in result)
        return vectors

    def max_batch_size(self) -> int:
        return int(self.client.get_max_batch_size())

    def upsert(self, chunks: list[dict[str, Any]], embeddings: list[list[float]] | None = None) -> None:
        if not chunks:
            return
        texts = [str(chunk["text"]) for chunk in chunks]
        vectors = embeddings if embeddings is not None else self.embed_texts(texts)
        if len(vectors) != len(chunks):
            raise RuntimeError("Embedding response count mismatch")
        self.collection.upsert(
            ids=[str(chunk["id"]) for chunk in chunks], documents=texts,
            metadatas=self.metadata(chunks),
            embeddings=[valid_vector(list(vector), self.embedding_dimension) for vector in vectors])

    def batch_digest(self, chunks: list[dict[str, Any]]) -> str | None:
        """Read back text, provenance and vectors before trusting a saved batch."""
        result = self.collection.get(ids=[row["id"] for row in chunks],
                                     include=["documents", "metadatas", "embeddings"])
        if len(result["ids"]) != len(chunks):
            return None
        positions = {identifier: i for i, identifier in enumerate(result["ids"])}
        rows = []
        for chunk, metadata in zip(chunks, self.metadata(chunks)):
            index = positions.get(chunk["id"])
            if (index is None or result["documents"][index] != chunk["text"]
                    or result["metadatas"][index] != metadata):
                return None
            try:
                vector = valid_vector(list(result["embeddings"][index]), self.embedding_dimension)
            except (TypeError, RuntimeError, ValueError):
                return None
            rows.append({"id": chunk["id"], "text": chunk["text"], "metadata": metadata,
                         "embedding": vector})
        return fingerprint(rows)

    def delete_source(self, source: str) -> None:
        # Chroma versions differ in how they handle a delete against an empty
        # collection. Treat an absent source as the idempotent operation it is.
        if self.count() == 0:
            return
        self.collection.delete(where={"source": source})

    def count(self) -> int:
        return int(self.collection.count())

    def search(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        count = self.count()
        if count == 0:
            return []
        query_args: dict[str, Any] = {
            "n_results": min(top_k, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if self.remote_embeddings:
            query_args["query_embeddings"] = self.embedder.embed([query])
        else:
            query_args["query_texts"] = [query]
        result = self.collection.query(**query_args)
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        output: list[dict[str, Any]] = []
        for identifier, document, metadata, distance in zip(ids, documents, metadatas, distances):
            output.append(
                {
                    "id": identifier,
                    "text": document,
                    "source": metadata["source"],
                    "source_sha256": metadata.get("source_sha256", ""),
                    "start_line": metadata.get("start_line"),
                    "end_line": metadata.get("end_line"),
                    "heading": metadata.get("heading", ""),
                    "chunk_type": metadata.get("chunk_type", "normal"),
                    "projection_kind": metadata.get("projection_kind"),
                    "projection_fingerprint": metadata.get("projection_fingerprint"),
                    "release_id": metadata.get("release_id"),
                    "release_hash": metadata.get("release_hash"),
                    "unit_ref": json.loads(metadata.get("unit_ref_json") or "null"),
                    "page_id": metadata.get("page_id") or None,
                    "page_revision_id": metadata.get("page_revision_id") or None,
                    "source_unit_refs": json.loads(metadata.get("source_unit_refs_json") or "[]"),
                    "subspan": json.loads(metadata.get("subspan_json") or "null"),
                    "subspan_reason": metadata.get("subspan_reason") or None,
                    "projection_content_sha256": metadata.get("projection_content_sha256") or "",
                    "score": 1 / (1 + float(distance)),
                }
            )
        return output
