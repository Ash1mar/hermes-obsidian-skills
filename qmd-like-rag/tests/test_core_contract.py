from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from qmd_like_rag.config import ProviderConfig
from qmd_like_rag.embeddings import OpenAIHttpEmbeddingBackend
from qmd_like_rag.contract import normalize_candidate
from qmd_like_rag.renderer import render_projections
from qmd_like_rag.reranker import OpenAIHttpReranker
from qmd_like_rag.runtime import read_status, recall


class FakeTokenizer:
    fingerprint = "sha256:tokenizer"
    max_tokens = 20

    @staticmethod
    def count(text: str) -> int:
        return len(text.split())


def projection(text: str = "supply system available") -> dict:
    ref = {"vault_id": "vault", "resource_id": "resource", "artifact_revision": "a" * 64,
           "unit_set_id": "b" * 64, "unit_id": "c" * 64}
    return {"projection_kind": "source_unit", "release_id": "release", "release_hash": "d" * 64,
            "unit_ref": ref, "page_revision_id": None, "source_unit_refs": [], "core_text": text,
            "heading_path": ["System", "Supply"], "vault_path": "_system/sources/a/document.md",
            "line_start": 2, "line_end": 2, "source_sha256": "e" * 64,
            "content_sha256": "f" * 64}


def test_renderer_keeps_one_canonical_unit_as_one_document() -> None:
    result = render_projections([projection()], FakeTokenizer(), "source-unit-renderer/v1")
    assert len(result) == 1
    assert result[0]["unit_ref"]["unit_id"] == "c" * 64
    assert result[0]["text"].startswith("Context: System / Supply")
    assert result[0]["projection_kind"] == "source_unit"


def test_renderer_blocks_normal_projection_over_model_limit() -> None:
    with pytest.raises(RuntimeError, match="exceeds embedding limit"):
        render_projections([projection("word " * 30)], FakeTokenizer(), "source-unit-renderer/v1")


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


def test_absent_recall_does_not_import_model_or_index_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__
    blocked = {"chromadb", "sentence_transformers", "torch"}

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in blocked:
            raise AssertionError(f"heavy runtime import attempted: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state")

    result = recall(config, "test")

    assert result["candidates"] == []
    assert result["warnings"] == ["index-not-ready"]


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
    assert main["embedding_tokenizer_path"].endswith("/tokenizer.json")
    assert "bge-reranker-large" in main["reranker_tokenizer_path"]
    assert intranet["state_root"] == "/opt/data/phq/qmd-like-rag-state"
    assert intranet["device"] == "cpu"
    assert intranet["embedding_backend"] == "openai_http"
    assert intranet["reranker_backend"] == "openai_http"
    assert intranet["model_audit_mode"] == "name-only"
    assert intranet["embedding_tokenizer_path"] == "/opt/models/bge-m3/tokenizer.json"
    assert intranet["reranker_tokenizer_path"] == "/opt/models/bge-reranker-v2-m3/tokenizer.json"
    assert intranet["embedding_revision"] is None
    assert intranet["reranker_revision"] is None
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
    assert "index-model-mismatch" in result["warnings"]


def test_p5_portable_config_removes_paths_and_legacy_chunk_fields(tmp_path: Path) -> None:
    config = ProviderConfig(vault_root=tmp_path / "vault", state_root=tmp_path / "state")
    portable = config.portable_dict()
    assert "include_patterns" not in portable
    assert "chunk_size" not in portable and "chunk_overlap" not in portable
    assert "embedding_tokenizer_path" not in portable
    assert portable["renderer_version"] == "source-unit-renderer/v1"
    assert config.model_manifest()["embedding_tokenizer"] == {
        "identity": "BAAI/bge-m3", "revision": None, "sha256": None, "max_tokens": 8192}


def remote_config(tmp_path: Path) -> ProviderConfig:
    return ProviderConfig(
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        embedding_backend="openai_http",
        embedding_endpoint="http://models.internal/v1/embeddings",
        embedding_model="bge-m3",
        embedding_dimension=3,
        embedding_batch_size=2,
        reranker_backend="openai_http",
        reranker_endpoint="http://models.internal/v1/rerank",
        reranker_model="bge-reranker-v2-m3",
        model_audit_mode="name-only",
    )


def test_remote_config_is_name_only_and_redacts_endpoints(tmp_path: Path) -> None:
    config = remote_config(tmp_path)
    portable = config.portable_dict()
    serialized = json.dumps(portable)
    assert "models.internal" not in serialized
    assert portable["embedding_endpoint_fingerprint"].startswith("sha256:")
    assert portable["reranker_endpoint_fingerprint"].startswith("sha256:")
    assert config.model_manifest()["embedding"] == {
        "identity": "bge-m3",
        "revision": None,
        "dimension": 3,
        "backend": "openai_http",
        "assurance": "name-only",
    }


def test_batch_embedding_uses_array_and_restores_index_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_post(endpoint, payload, **kwargs):
        calls.append(payload)
        return {
            "object": "list",
            "model": "/srv/models/bge-m3",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [4, 5, 6]},
                {"object": "embedding", "index": 0, "embedding": [1, 2, 3]},
            ],
        }

    monkeypatch.setattr("qmd_like_rag.embeddings.post_json", fake_post)
    result = OpenAIHttpEmbeddingBackend(remote_config(tmp_path)).embed(["甲", "乙"])
    assert calls == [{"input": ["甲", "乙"]}]
    assert result == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


def test_batch_embedding_rejects_wrong_dimension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "qmd_like_rag.embeddings.post_json",
        lambda *args, **kwargs: {"data": [{"index": 0, "embedding": [1, 2]}]},
    )
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        OpenAIHttpEmbeddingBackend(remote_config(tmp_path)).embed(["甲"])


def test_remote_reranker_maps_results_by_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "qmd_like_rag.reranker.post_json",
        lambda endpoint, payload, **kwargs: {
            "id": "rerank-test",
            "model": "bge-reranker-v2-m3",
            "results": [
                {"index": 1, "document": {"text": "乙"}, "relevance_score": 0.9},
                {"index": 0, "document": {"text": "甲"}, "relevance_score": 0.2},
            ],
        },
    )
    result = OpenAIHttpReranker(remote_config(tmp_path)).rerank(
        "问题", [{"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}], 2
    )
    assert [item["id"] for item in result] == ["b", "a"]
    assert all(item["score_type"] == "remote-cross-encoder" for item in result)
