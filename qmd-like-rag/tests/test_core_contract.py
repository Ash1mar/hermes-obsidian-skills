from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from qmd_like_rag.chunker import chunk_markdown_file
from qmd_like_rag.config import ProviderConfig
from qmd_like_rag.contract import normalize_candidate
from qmd_like_rag.corpus import resolve_sources
from qmd_like_rag.runtime import read_status


def test_default_corpus_selects_governed_and_document_markdown(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    included = [
        vault / "30_Cards" / "card.md",
        vault / "_system" / "reports" / "manual.source-map.md",
        vault / "10_Raw" / "converted" / "manual_document_bundle" / "document.md",
    ]
    excluded = [
        vault / "_system" / "reports" / "query-traces" / "trace.md",
        vault / "10_Raw" / "converted" / "manual_document_bundle" / "tables" / "table.md",
    ]
    for path in included + excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n", encoding="utf-8")
    config = ProviderConfig(vault_root=vault, state_root=tmp_path / "state")
    assert [item.path for item in resolve_sources(vault.resolve(), config.include_patterns)] == [
        path.resolve() for path in sorted(included, key=lambda item: item.resolve().relative_to(vault.resolve()).as_posix())
    ]


def test_chinese_markdown_is_chunked_and_keeps_stable_source_location(tmp_path: Path) -> None:
    path = tmp_path / "document.md"
    path.write_text("# 系统要求\n\n" + "供水系统应保持可用。" * 200, encoding="utf-8")
    chunks = chunk_markdown_file(
        path,
        source_id="10_Raw/converted/example/document.md",
        source_sha256="abc",
        chunk_size=80,
        overlap_ratio=0.1,
    )
    assert len(chunks) > 1
    assert {item["source"] for item in chunks} == {"10_Raw/converted/example/document.md"}
    assert all(item["start_line"] == 1 and item["end_line"] == 3 for item in chunks)
    assert all(len(item["id"]) == 64 for item in chunks)


def test_candidate_contract_rejects_paths_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError):
        normalize_candidate({"source": str(tmp_path / "outside.md")}, vault)
    with pytest.raises(ValueError):
        normalize_candidate({"source": "../outside.md"}, vault)


def test_absent_status_does_not_load_heavy_runtime(tmp_path: Path) -> None:
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state")
    result = read_status(config)
    assert result["status"] == "absent"
    assert result["protocol_version"] == "hermes-coarse-recall/v1"
    assert result["model_fingerprint"].startswith("identity-sha256:")


def test_short_vault_id_still_produces_valid_chroma_name(tmp_path: Path) -> None:
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state", vault_id="x")
    assert config.collection_name == "vault-x"
