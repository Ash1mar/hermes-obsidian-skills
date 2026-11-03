"""Rebuildable, checksum-validated vectors scoped to one Vault and embedding identity."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path


def valid_vector(vector: object, dimension: int) -> list[float]:
    if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
        raise RuntimeError("Embedding dimension mismatch")
    converted = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in converted):
        raise RuntimeError("Embedding contains a non-finite value")
    return converted


class EmbeddingCache:
    def __init__(self, path: Path, namespace: str, dimension: int) -> None:
        self.namespace, self.dimension = namespace, dimension
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS vectors (namespace TEXT, text_hash TEXT, "
            "payload TEXT NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY(namespace, text_hash))")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> list[float] | None:
        row = self.connection.execute(
            "SELECT payload, checksum FROM vectors WHERE namespace=? AND text_hash=?",
            (self.namespace, self.text_hash(text))).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            return None
        try:
            return valid_vector(json.loads(row[0]), self.dimension)
        except (ValueError, TypeError, RuntimeError):
            return None

    def put(self, texts: list[str], vectors: list[list[float]]) -> None:
        if len(texts) != len(vectors):
            raise RuntimeError("Embedding response count mismatch")
        rows = []
        for text, vector in zip(texts, vectors):
            payload = json.dumps(valid_vector(vector, self.dimension), separators=(",", ":"))
            rows.append((self.namespace, self.text_hash(text), payload,
                         hashlib.sha256(payload.encode()).hexdigest()))
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)", rows)
