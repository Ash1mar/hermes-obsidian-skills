from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import qmd_like_rag.corpus as corpus_module
from qmd_like_rag.config import ProviderConfig
from qmd_like_rag.corpus import load_release_corpus
from qmd_like_rag.tokenization import ModelTokenizer
from qmd_like_rag.renderer import render_projections


REF = {"vault_id": "vault", "resource_id": "resource", "artifact_revision": "a" * 64,
       "unit_set_id": "b" * 64, "unit_id": "c" * 64}


class FakeSourceService:
    def __init__(self, vault_root: Path):
        self.vault_manifest = {"governance": {"repository": {"registry_path": "registry.json"}}}

    def validate(self, resource_id: str, unit_set_id: str):
        assert (resource_id, unit_set_id) == ("resource", "b" * 64)

    def list(self, resource_id: str, unit_set_id: str):
        return [{"ref": REF, "locator": {"kind": "text", "path": "document.md",
                                           "line_start": 4, "line_end": 5,
                                           "span": {"start": 0, "end": 30}},
                 "heading_path": ["Requirements"], "source_sha256": "d" * 64,
                 "content_sha256": "e" * 64, "content_type": "normalized_source",
                 "quality_refs": ["_system/source-units/resource/report/engine.json"]}]

    def get(self, request):
        assert request["source_ref"]["unit_ref"] == REF
        return {"core_text": "System shall remain available.", "content_sha256": "e" * 64}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def test_release_corpus_enumerates_units_and_pages_without_markdown_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write_json(vault / "registry.json", {"registry_revision": 7})
    write_json(vault / "_system/source-units/resource/report/engine.json",
               {"diagnostics": [{"code": "oversized-protected-structure",
                                  "span": {"start": 0, "end": 30}}]})
    page = vault / "30_Cards" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n\nDerived knowledge.\n", encoding="utf-8")
    page_hash = hashlib.sha256(page.read_bytes()).hexdigest()
    write_json(vault / "_system/knowledge-builds/page-revisions/page/revision.json",
               {"page_id": "page", "revision_id": "revision", "state": "committed",
                "authored_sha256": page_hash})
    release = {"contract": "hermes-knowledge-release/v1", "release_id": "release",
               "state": "completed", "subjects": [{"page_id": "page", "current_path": "30_Cards/page.md"}],
               "index_eligibility": [
                   {"kind": "source_unit_set", "id": "b" * 64, "eligible": True,
                    "resource_id": "resource", "unit_set_id": "b" * 64},
                   {"kind": "knowledge_page", "id": "page", "eligible": True,
                    "page_revision_id": "revision", "source_unit_refs": [{"unit_ref": REF, "span": None}]},
                   {"kind": "source_unit_set", "id": "ignored", "eligible": False},
               ]}
    write_json(vault / "_system/knowledge-releases/release/manifest.json", release)
    write_json(vault / "_system/metadata/knowledge-release-state.json", {"current_release_id": "release"})
    monkeypatch.setattr(corpus_module, "FileSourceUnitService", FakeSourceService)

    result = load_release_corpus(vault)

    assert result.release_id == "release"
    assert [item["projection_kind"] for item in result.projections] == ["source_unit", "knowledge_page"]
    assert result.projections[0]["unit_ref"] == REF
    assert result.projections[0]["vault_path"].endswith("/document.md")
    assert result.projections[0]["oversized_protected_spans"] == [{"start": 0, "end": 30}]
    assert result.projections[1]["page_revision_id"] == "revision"


def test_model_tokenizer_verifies_publisher_asset_checksum(tmp_path: Path) -> None:
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0, "hello": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    config = ProviderConfig(vault_root=tmp_path / "vault", embedding_tokenizer_path=path,
                            embedding_tokenizer_sha256=digest, embedding_max_tokens=8)
    loaded = ModelTokenizer(config)
    assert loaded.count("hello hello") == 2
    assert loaded.manifest()["sha256"] == digest
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        ModelTokenizer(ProviderConfig(vault_root=tmp_path / "vault", embedding_tokenizer_path=path,
                                      embedding_tokenizer_sha256="0" * 64))


class TinyTokenizer:
    max_tokens = 5
    fingerprint = "sha256:tiny"

    @staticmethod
    def count(text: str) -> int:
        return len(text)

    def split(self, text: str, prefix: str = "") -> list[tuple[int, int]]:
        width = self.max_tokens - len(prefix)
        return [(start, min(len(text), start + width)) for start in range(0, len(text), width)]


def projection(**overrides):
    value = {
        "projection_kind": "source_unit", "unit_ref": REF, "page_id": None,
        "page_revision_id": None, "source_unit_refs": [], "core_text": "abcdefghij",
        "heading_path": [], "vault_path": "_system/sources/artifacts/resource/rev/document.md",
        "line_start": 1, "line_end": 1, "source_sha256": "d" * 64,
        "content_sha256": "e" * 64, "release_id": "release", "release_hash": "f" * 64,
        "locator_span": {"start": 20, "end": 30}, "oversized_protected_spans": [],
    }
    value.update(overrides)
    return value


def test_renderer_rejects_ordinary_unit_over_model_limit() -> None:
    with pytest.raises(RuntimeError, match="exceeds embedding limit"):
        render_projections([projection()], TinyTokenizer(), "source-unit-projection/v1")


def test_renderer_uses_exact_nonoverlapping_subspans_only_for_oversized_protected_unit() -> None:
    rows = render_projections([
        projection(oversized_protected_spans=[{"start": 20, "end": 30}])
    ], TinyTokenizer(), "source-unit-projection/v1")
    assert [row["subspan"] for row in rows] == [
        {"start": 20, "end": 25}, {"start": 25, "end": 30}]
    assert "".join(row["core_text"] for row in rows) == "abcdefghij"
    assert len({row["projection_fingerprint"] for row in rows}) == 2
    assert all(row["unit_ref"] == REF and row["token_count"] <= 5 for row in rows)
    assert all(row["subspan_reason"] == "oversized-protected-structure" for row in rows)
    assert all(row["projection_content_sha256"] == hashlib.sha256(
        row["core_text"].encode("utf-8")).hexdigest() for row in rows)
