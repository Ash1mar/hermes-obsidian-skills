from __future__ import annotations

import math
from typing import Any

from .model_http import post_json


class OpenAIHttpEmbeddingBackend:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.embedding_dimension = int(config.embedding_dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        output: list[list[float]] = []
        for offset in range(0, len(texts), self.config.embedding_batch_size):
            batch = texts[offset : offset + self.config.embedding_batch_size]
            payload: dict[str, Any] = {"input": batch}
            if self.config.embedding_request_model:
                payload["model"] = self.config.embedding_request_model
            response = post_json(
                self.config.embedding_endpoint,
                payload,
                timeout=self.config.model_request_timeout_seconds,
                max_retries=self.config.model_request_max_retries,
                api_key_env=self.config.embedding_api_key_env,
            )
            output.extend(self._parse(response, len(batch)))
        return output

    def _parse(self, response: dict[str, Any], expected: int) -> list[list[float]]:
        data = response.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise RuntimeError(f"Embedding response count mismatch: expected {expected}")
        ordered: list[list[float] | None] = [None] * expected
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise RuntimeError("Embedding response item requires an integer index")
            index = item["index"]
            vector = item.get("embedding")
            if index < 0 or index >= expected or ordered[index] is not None:
                raise RuntimeError("Embedding response contains an invalid or duplicate index")
            if not isinstance(vector, list) or len(vector) != self.embedding_dimension:
                raise RuntimeError(
                    "Embedding dimension mismatch: "
                    f"expected {self.embedding_dimension}, got {len(vector) if isinstance(vector, list) else 'invalid'}"
                )
            converted = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in converted):
                raise RuntimeError("Embedding response contains a non-finite value")
            ordered[index] = converted
        if any(vector is None for vector in ordered):
            raise RuntimeError("Embedding response omitted an index")
        return [vector for vector in ordered if vector is not None]
