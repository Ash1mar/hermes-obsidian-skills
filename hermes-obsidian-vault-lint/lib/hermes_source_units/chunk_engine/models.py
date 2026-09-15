"""Immutable values exchanged by the shared Chunk Engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


class TokenCounter(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class ChunkProfile:
    strategy: str
    target_codepoints: int
    max_codepoints: int
    overlap_codepoints: int
    separators: tuple[str, ...]
    languages: tuple[str, ...]
    token_mode: str
    max_tokens: int
    tokenizer_fingerprint: str | None
    oversized_policy: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChunkProfile":
        budget = value["token_budget"]
        return cls(
            strategy=str(value["strategy"]),
            target_codepoints=int(value["target_codepoints"]),
            max_codepoints=int(value["max_codepoints"]),
            overlap_codepoints=int(value["overlap_codepoints"]),
            separators=tuple(str(item) for item in value["separators"]),
            languages=tuple(str(item) for item in value["languages"]),
            token_mode=str(budget["mode"]),
            max_tokens=int(budget["max_tokens"]),
            tokenizer_fingerprint=budget["tokenizer_fingerprint"],
            oversized_policy=str(value["oversized_policy"]),
        )


@dataclass(frozen=True)
class ChunkEngineInput:
    text: str
    outline: Mapping[str, Any]
    assets: Sequence[Mapping[str, Any]]
    artifact_revision: str
    profile: ChunkProfile


@dataclass(frozen=True)
class CanonicalChunkSpan:
    section_id: str
    start: int
    end: int
    structure_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkEngineResult:
    sections: tuple[dict[str, Any], ...]
    section_metadata: Mapping[str, dict[str, Any]]
    chunks: tuple[CanonicalChunkSpan, ...]
    diagnostics: tuple[dict[str, Any], ...]
    statistics: Mapping[str, Any]
    report: Mapping[str, Any]
    engine_fingerprint: str
