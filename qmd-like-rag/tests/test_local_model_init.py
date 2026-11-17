import sys
from types import ModuleType, SimpleNamespace

import pytest

from qmd_like_rag.local_models import cached_model_path
from qmd_like_rag.reranker import BgeReranker
from qmd_like_rag.storage.chroma_store import sentence_transformer_embedding


def test_model_loaders_use_only_the_pinned_snapshot_and_keep_chroma_identity(tmp_path, monkeypatch):
    calls = []
    snapshot = str(tmp_path / "cached-snapshot")

    class LocalEntryNotFoundError(Exception):
        pass

    def download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        if repo_id == "owner/missing":
            raise LocalEntryNotFoundError
        return snapshot

    def embedding(model_name, **kwargs):
        return SimpleNamespace(model_name=model_name, loaded_from=model_name, kwargs=kwargs)

    def reranker(model_name, **kwargs):
        return SimpleNamespace(loaded_from=model_name, kwargs=kwargs)

    for name, attrs in {
        "huggingface_hub": {"snapshot_download": download},
        "huggingface_hub.errors": {"LocalEntryNotFoundError": LocalEntryNotFoundError},
        "chromadb.utils": {"embedding_functions": SimpleNamespace(SentenceTransformerEmbeddingFunction=embedding)},
        "sentence_transformers": {"CrossEncoder": reranker},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "/embedding-cache")
    monkeypatch.setenv("TRANSFORMERS_CACHE", "/transformers-cache")
    config = SimpleNamespace(embedding_model="owner/embedding", embedding_revision="a" * 40,
                             local_files_only=True, device="cpu")
    embed = sentence_transformer_embedding(config)
    assert embed.loaded_from == snapshot
    assert embed.model_name == config.embedding_model
    assert embed.kwargs == {"device": "cpu", "revision": "a" * 40, "local_files_only": True}
    ranker = BgeReranker("owner/reranker", "cpu", revision="b" * 40, local_files_only=True)
    assert ranker.model.loaded_from == snapshot
    assert calls == [
        ("owner/embedding", {"revision": "a" * 40, "cache_dir": "/embedding-cache", "local_files_only": True}),
        ("owner/reranker", {"revision": "b" * 40, "cache_dir": "/transformers-cache", "local_files_only": True}),
    ]

    config.local_files_only = False
    assert sentence_transformer_embedding(config).loaded_from == config.embedding_model
    assert BgeReranker("owner/reranker", "cpu").model.loaded_from == "owner/reranker"
    assert len(calls) == 2
    assert cached_model_path(str(tmp_path), None) == str(tmp_path.resolve())
    with pytest.raises(RuntimeError, match="Local model snapshot is missing.*owner/missing"):
        cached_model_path("owner/missing", "c" * 40)
    assert calls[-1] == ("owner/missing", {"revision": "c" * 40, "cache_dir": None, "local_files_only": True})
