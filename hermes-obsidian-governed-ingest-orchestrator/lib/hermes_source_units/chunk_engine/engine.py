"""Strategy selection, validation, fallback and auditable reporting."""
from __future__ import annotations

from typing import Any

from ..validation import ContractError, fingerprint
from .models import ChunkEngineInput, ChunkEngineResult, ChunkProfile, TokenCounter
from .overlap import apply_overlap
from .profile import document_profile, heuristic_outline
from .sections import build_sections
from .strategies import build_base_spans
from .validation import validate_candidate

ENGINE_VERSION = "hermes-shared-chunk-engine/1"
ENGINE_FINGERPRINT = fingerprint({
    "engine_version": ENGINE_VERSION,
    "pipeline": ["profile", "sections-owned-ranges", "atoms", "strategies", "overlap", "validation"],
    "strategies": ["structure", "heuristic", "recursive"],
    "offsets": "lf-unicode-codepoint-half-open",
    "oversized": "preserve-and-report",
})


def _profile_payload(profile: ChunkProfile) -> dict[str, Any]:
    return {
        "strategy": profile.strategy, "target_codepoints": profile.target_codepoints,
        "max_codepoints": profile.max_codepoints, "overlap_codepoints": profile.overlap_codepoints,
        "separators": list(profile.separators), "languages": list(profile.languages),
        "token_budget": {"mode": profile.token_mode, "max_tokens": profile.max_tokens,
                         "tokenizer_fingerprint": profile.tokenizer_fingerprint},
        "oversized_policy": profile.oversized_policy,
    }


class SharedChunkEngine:
    def __init__(self, token_counter: TokenCounter | None = None):
        self.token_counter = token_counter

    def _chain(self, requested: str, has_outline: bool) -> list[str]:
        if requested == "auto":
            return (["structure"] if has_outline else []) + ["heuristic", "recursive"]
        if requested == "structure":
            return ["structure", "heuristic", "recursive"]
        if requested == "heuristic":
            return ["heuristic", "recursive"]
        return ["recursive"]

    def build(self, value: ChunkEngineInput) -> ChunkEngineResult:
        if value.profile.token_mode == "hard":
            if self.token_counter is None:
                raise ContractError("TOKENIZER_REQUIRED", "$.source.chunking.token_budget",
                                    "hard token mode requires a TokenCounter")
            if value.profile.tokenizer_fingerprint != self.token_counter.fingerprint:
                raise ContractError("TOKENIZER_MISMATCH", "$.source.chunking.token_budget.tokenizer_fingerprint",
                                    "configured tokenizer fingerprint differs from the supplied TokenCounter")
        doc_profile = document_profile(value.text, value.outline)
        attempts: list[dict[str, Any]] = []
        selected_result = None
        for strategy in self._chain(value.profile.strategy, bool(doc_profile["outline_sections"])):
            if strategy == "structure":
                outline = value.outline
                if not doc_profile["outline_sections"]:
                    attempts.append({"strategy": strategy, "accepted": False, "reasons": ["no reliable outline sections"]})
                    continue
            elif strategy == "heuristic":
                outline = heuristic_outline(value.text)
                if not outline["sections"]:
                    attempts.append({"strategy": strategy, "accepted": False, "reasons": ["no heuristic boundaries"]})
                    continue
            else:
                outline = {"schema_version": "source-outline/2", "document": "document.md", "sections": []}
            try:
                sections, metadata = build_sections(value.text, outline, value.artifact_revision)
                base, diagnostics, protected = build_base_spans(value.text, sections, value.profile, self.token_counter)
                chunks = apply_overlap(value.text, base, value.profile, protected, self.token_counter, diagnostics)
                reasons, statistics = validate_candidate(value.text, sections, chunks, value.profile, diagnostics)
            except ContractError as exc:
                attempts.append({"strategy": strategy, "accepted": False,
                                 "reasons": [f"{exc.code}: {exc.message}"]})
                continue
            accepted = not reasons or (strategy == "recursive" and set(reasons) <= {"excessive tiny chunk ratio"})
            attempts.append({"strategy": strategy, "accepted": accepted, "reasons": reasons})
            if accepted:
                selected_result = (strategy, sections, metadata, chunks, diagnostics, statistics, reasons)
                break
        if selected_result is None:
            raise ContractError("INVALID_CHUNKS", "$", "all configured chunk strategies failed")
        strategy, sections, metadata, chunks, diagnostics, statistics, final_reasons = selected_result
        diagnostics.insert(0, {"code": "strategy-selected", "requested": value.profile.strategy,
                               "selected": strategy, "attempted": [item["strategy"] for item in attempts]})
        if final_reasons:
            diagnostics.append({"code": "preserve-and-report", "reasons": final_reasons})
        token_counts = ([self.token_counter.count(value.text[item.start:item.end]) for item in chunks]
                        if self.token_counter and value.profile.token_mode != "off" else [])
        token_audit = {
            "mode": value.profile.token_mode,
            "status": ("disabled" if value.profile.token_mode == "off" else
                       "complete" if self.token_counter else "unavailable"),
            "tokenizer_fingerprint": self.token_counter.fingerprint if self.token_counter else value.profile.tokenizer_fingerprint,
            "max_tokens": value.profile.max_tokens,
            "maximum_observed_tokens": max(token_counts, default=None),
            "over_limit_count": sum(count > value.profile.max_tokens for count in token_counts),
        }
        report = {
            "contract": "hermes-chunk-engine-report/v1", "engine_version": ENGINE_VERSION,
            "engine_fingerprint": ENGINE_FINGERPRINT, "artifact_revision": value.artifact_revision,
            "effective_config": _profile_payload(value.profile), "document_profile": doc_profile,
            "attempts": attempts, "selected_strategy": strategy,
            "statistics": statistics, "token_audit": token_audit,
            "diagnostics": diagnostics,
            "coverage": {"complete": statistics["coverage_complete"],
                         "covered_codepoints": statistics["covered_codepoints"],
                         "document_codepoints": len(value.text)},
        }
        return ChunkEngineResult(tuple(sections), metadata, tuple(chunks), tuple(diagnostics),
                                 statistics, report, ENGINE_FINGERPRINT)
