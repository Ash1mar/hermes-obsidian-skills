from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from qmd_like_rag.chunker import chunk_markdown_file
from qmd_like_rag.config import ProviderConfig
from qmd_like_rag.contract import normalize_candidate
from qmd_like_rag.corpus import resolve_sources
from qmd_like_rag.runtime import read_status, recall


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
    assert result["configuration"]["embedding_model"] == "BAAI/bge-m3"
    assert result["model_fingerprint"].startswith("sha256:")
    assert result["models"]["embedding"]["identity"] == "BAAI/bge-m3"


def test_short_vault_id_still_produces_valid_chroma_name(tmp_path: Path) -> None:
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state", vault_id="x")
    assert config.collection_name == "vault-x"


def test_branch_examples_keep_provider_state_outside_the_vault() -> None:
    main = json.loads((PACKAGE_ROOT / "config" / "main.example.json").read_text(encoding="utf-8"))
    intranet = json.loads(
        (PACKAGE_ROOT / "config" / "intranet.example.json").read_text(encoding="utf-8")
    )
    assert main["state_root"] == "/root/.local/state/qmd-like-rag"
    assert main["device"] == "cuda"
    assert main["local_files_only"] is True
    assert len(main["embedding_revision"]) == 40
    assert len(main["reranker_revision"]) == 40
    assert main["embedding_dimension"] == 1024
    assert intranet["state_root"] == "/opt/data/phq/qmd-like-rag-state"
    assert intranet["device"] == "cpu"
    assert not intranet["state_root"].startswith("/opt/data/phq/testVault/")


def test_provider_rejects_unknown_execution_device(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="device"):
        ProviderConfig(vault_root=tmp_path / "vault", device="automatic")


def test_immutable_model_audit_requires_full_revisions_and_dimension(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="embedding_revision"):
        ProviderConfig(
            vault_root=tmp_path / "vault",
            require_immutable_model_revisions=True,
            embedding_revision="main",
            reranker_revision="0" * 40,
            embedding_dimension=1024,
        )
    with pytest.raises(ValueError, match="embedding_dimension"):
        ProviderConfig(
            vault_root=tmp_path / "vault",
            require_immutable_model_revisions=True,
            embedding_revision="0" * 40,
            reranker_revision="1" * 40,
        )


def test_recall_rejects_an_index_built_with_different_models(tmp_path: Path) -> None:
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state")
    config.ensure_dirs()
    state = read_status(config)
    state.update(
        {
            "status": "ready",
            "configuration_fingerprint": config.config_fingerprint(),
            "model_fingerprint": "sha256:stale",
            "index_fingerprint": "sha256:index",
        }
    )
    config.state_path().write_text(json.dumps(state), encoding="utf-8")

    result = recall(config, "test")

    assert result["candidates"] == []
    assert result["warnings"] == ["index-model-mismatch"]
