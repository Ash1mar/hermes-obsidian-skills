"""P2.1 shared Chunk Engine strategy, protection and token-budget acceptance."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-source-units/src"))

from hermes_source_units import (ChunkEngineInput, ChunkProfile, ContractError,
                                 SharedChunkEngine)

REVISION = "a" * 64


class CharacterCounter:
    def __init__(self, fingerprint: str = "test-character-tokenizer/v1"):
        self._fingerprint = fingerprint

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def count(self, text: str) -> int:
        return len(text)


def profile(**changes) -> ChunkProfile:
    value = {
        "strategy": "auto", "target_codepoints": 40, "max_codepoints": 60,
        "overlap_codepoints": 8, "separators": ["\n\n", "\n", "。", ". "],
        "languages": ["zh", "en"],
        "token_budget": {"mode": "audit", "max_tokens": 100,
                         "tokenizer_fingerprint": None},
        "oversized_policy": "preserve-and-report",
    }
    value.update(changes)
    return ChunkProfile.from_mapping(value)


def build(text: str, chunk_profile: ChunkProfile, outline=None, counter=None):
    return SharedChunkEngine(counter).build(ChunkEngineInput(
        text=text, outline=outline or {"sections": []}, assets=[],
        artifact_revision=REVISION, profile=chunk_profile,
    ))


def test_auto_prefers_reliable_structure_and_reports_complete_coverage():
    text = "# Alpha\nalpha body\n# Beta\nbeta body\n"
    outline = {"sections": [
        {"id": "alpha", "title": "Alpha", "level": 1, "start_line": 1, "end_line": 2,
         "path": ["Alpha"], "pages": [], "assets": [], "quality": "pass"},
        {"id": "beta", "title": "Beta", "level": 1, "start_line": 3, "end_line": 4,
         "path": ["Beta"], "pages": [], "assets": [], "quality": "pass"},
    ]}
    result = build(text, profile(), outline)
    assert result.report["selected_strategy"] == "structure"
    assert result.report["attempts"] == [{"strategy": "structure", "accepted": True, "reasons": []}]
    assert result.report["coverage"] == {
        "complete": True, "covered_codepoints": len(text), "document_codepoints": len(text)}


def test_auto_uses_heuristic_then_recursive_fallback_is_explainable():
    heuristic = build("前言。\n\n一、范围\n内容。\n\n二、例外\n说明。\n", profile())
    assert heuristic.report["selected_strategy"] == "heuristic"
    assert heuristic.report["document_profile"]["language_signal"] == "cjk"

    lines = [f"Topic {index}\n\n" for index in range(10)]
    text = "".join(lines)
    outline = {"sections": [
        {"id": f"s{index}", "title": f"Topic {index}", "level": 1,
         "start_line": index * 2 + 1, "end_line": index * 2 + 1,
         "path": [f"Topic {index}"], "pages": [], "assets": [], "quality": "pass"}
        for index in range(10)
    ]}
    fallback = build(text, profile(target_codepoints=80, max_codepoints=100), outline)
    assert fallback.report["selected_strategy"] == "recursive"
    assert [item["strategy"] for item in fallback.report["attempts"]] == [
        "structure", "heuristic", "recursive"]
    assert "excessive tiny chunk ratio" in fallback.report["attempts"][0]["reasons"]


def test_oversized_protected_structure_is_preserved_and_reported():
    code = "```python\n" + "x = 1\n" * 20 + "```\n"
    result = build(code, profile(target_codepoints=30, max_codepoints=50))
    assert len(result.chunks) == 1
    assert result.chunks[0].start == 0 and result.chunks[0].end == len(code)
    assert "code" in result.chunks[0].structure_kinds
    issue = next(item for item in result.diagnostics if item["code"] == "oversized-protected-structure")
    assert issue["kind"] == "code"


def test_hard_token_mode_splits_plain_text_and_checks_counter_identity():
    counter = CharacterCounter()
    hard = profile(target_codepoints=50, max_codepoints=100, overlap_codepoints=0,
                   token_budget={"mode": "hard", "max_tokens": 10,
                                 "tokenizer_fingerprint": counter.fingerprint})
    text = "abcdefghij" * 3
    result = build(text, hard, counter=counter)
    assert all(counter.count(text[item.start:item.end]) <= 10 for item in result.chunks)
    assert result.report["token_audit"]["status"] == "complete"
    assert result.report["token_audit"]["over_limit_count"] == 0

    with pytest.raises(ContractError) as mismatch:
        build("content", hard, counter=CharacterCounter("different/v1"))
    assert mismatch.value.code == "TOKENIZER_MISMATCH"


def test_audit_mode_without_counter_is_explicitly_unavailable():
    result = build("plain content", profile())
    assert result.report["token_audit"]["status"] == "unavailable"
    assert result.report["token_audit"]["maximum_observed_tokens"] is None


def test_heuristic_ignores_numbered_lines_inside_fenced_code():
    text = "```text\n\n一、这不是标题\n\n```\nordinary body\n"
    result = build(text, profile())
    assert result.report["selected_strategy"] == "recursive"
    assert result.report["attempts"][0] == {
        "strategy": "heuristic", "accepted": False, "reasons": ["no heuristic boundaries"]}
