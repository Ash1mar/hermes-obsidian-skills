from __future__ import annotations

from typing import Any


class ChromaStore:
    def __init__(self, config: Any) -> None:
        try:
            import chromadb
            from chromadb.config import Settings
            from chromadb.utils import embedding_functions
        except ImportError as exc:
            raise RuntimeError("Install qmd-like-rag runtime dependencies before using Chroma") from exc

        self.config = config
        self.path = config.chroma_path()
        self.path.mkdir(parents=True, exist_ok=True)
        self.embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=config.embedding_model,
            device=config.device,
            revision=config.embedding_revision,
            local_files_only=config.local_files_only,
        )
        self.embedding_dimension = int(self.embed_fn._model.get_sentence_embedding_dimension())
        if (
            config.embedding_dimension is not None
            and self.embedding_dimension != config.embedding_dimension
        ):
            raise RuntimeError(
                "Embedding dimension mismatch: "
                f"expected {config.embedding_dimension}, got {self.embedding_dimension}"
            )
        self.client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=config.collection_name,
            embedding_function=self.embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self) -> None:
        try:
            self.client.delete_collection(self.config.collection_name)
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name=self.config.collection_name,
            embedding_function=self.embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            return
        ids = [str(chunk["id"]) for chunk in chunks]
        documents = [str(chunk["text"]) for chunk in chunks]
        metadata = [
            {
                "source": str(chunk["source"]),
                "source_sha256": str(chunk["source_sha256"]),
                "start_line": int(chunk["start_line"]),
                "end_line": int(chunk["end_line"]),
                "parent_id": str(chunk["parent_id"]),
                "parent_text": str(chunk["parent_text"]),
                "heading": str(chunk.get("heading", "")),
                "chunk_type": str(chunk.get("chunk_type", "normal")),
            }
            for chunk in chunks
        ]
        self.collection.upsert(ids=ids, documents=documents, metadatas=metadata)

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
        result = self.collection.query(
            query_texts=[query],
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )
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
                    "parent_id": metadata.get("parent_id"),
                    "parent_text": metadata.get("parent_text"),
                    "heading": metadata.get("heading", ""),
                    "chunk_type": metadata.get("chunk_type", "normal"),
                    "score": 1 / (1 + float(distance)),
                }
            )
        return output
