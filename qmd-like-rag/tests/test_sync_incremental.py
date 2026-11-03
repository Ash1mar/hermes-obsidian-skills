from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

if os.environ.get("QMD_LIKE_RAG_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qmd_like_rag.indexer as indexing
import qmd_like_rag.runtime as runtime
import qmd_like_rag.service as service
from qmd_like_rag.config import ProviderConfig
from qmd_like_rag.corpus import ReleaseCorpus
from qmd_like_rag.storage.chroma_store import ChromaStore
from qmd_like_rag.storage.embedding_cache import EmbeddingCache
from qmd_like_rag.sync_support import bounded_batches, fingerprint, read_json, write_atomic, writer_lock


class TestTokenizer:
    __test__ = False
    max_tokens = 100

    def __init__(self, config, role="embedding"):
        self.fingerprint = fingerprint({"role": role, "revision": getattr(config, role + "_revision")})

    def count(self, text):
        return len(text.split())

    def manifest(self):
        return {"fingerprint": self.fingerprint, "ready": True}


def projection(number, text, release):
    return {
        "projection_kind": "source_unit", "release_id": release, "release_hash": fingerprint(release),
        "unit_ref": {"vault_id": "vault", "resource_id": "resource", "artifact_revision": "a" * 64,
                     "unit_set_id": "b" * 64, "unit_id": hashlib.sha256(str(number).encode()).hexdigest()},
        "page_revision_id": None, "source_unit_refs": [], "core_text": text, "heading_path": [],
        "vault_path": "_system/sources/artifacts/resource/revision/document.md",
        "line_start": number + 1, "line_end": number + 1, "source_sha256": "c" * 64,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    config = ProviderConfig(
        vault_root=tmp_path / "vault", state_root=tmp_path / "provider", vault_id="sync-test",
        embedding_backend="openai_http", embedding_endpoint="http://unused.invalid/v1/embeddings",
        embedding_model="fixture-model", embedding_dimension=3, embedding_revision="1" * 40,
        reranker_revision="2" * 40, model_audit_mode="immutable-revision", use_reranker=False,
        index_batch_size=2, embedding_batch_size=2, embedding_batch_max_tokens=8)
    state = SimpleNamespace(config=config, calls=[], fail_at=None, corpus=None)

    def release(identity, texts):
        state.corpus = ReleaseCorpus(identity, fingerprint(identity), {}, tuple(
            projection(i, text, identity) for i, text in enumerate(texts)))
        return state.corpus

    state.release = release
    release("r1", ["alpha requirement", "beta requirement", "gamma requirement",
                   "delta requirement", "epsilon requirement"])
    monkeypatch.setattr(indexing, "ModelTokenizer", TestTokenizer)
    monkeypatch.setattr(runtime, "ModelTokenizer", TestTokenizer)
    monkeypatch.setattr(indexing, "load_release_corpus", lambda root: state.corpus)
    identity = lambda root: (state.corpus.release_id, state.corpus.release_hash)
    monkeypatch.setattr(indexing, "current_release_identity", identity)
    monkeypatch.setattr(runtime, "current_release_identity", identity)

    def post(endpoint, payload, **kwargs):
        state.calls.append(payload["input"])
        if state.fail_at == len(state.calls):
            raise RuntimeError("isolated embedding failure")
        return {"data": [{"index": i, "embedding": [float(hashlib.sha256(text.encode()).digest()[0]), 1., 2.]}
                         for i, text in enumerate(payload["input"])]}

    monkeypatch.setattr("qmd_like_rag.embeddings.post_json", post)
    return state


def sync(s, config=None, **kwargs):
    return indexing.HybridIndexer(config or s.config).sync(**kwargs)


def test_batches_bound_embedding_and_writes_and_publish_timings(scenario):
    result = sync(scenario)
    assert [len(call) for call in scenario.calls] == [2, 2, 1]
    assert result["document_count"] == 5
    assert result["sync_metrics"]["embedded_documents"] == 5
    assert {"validation", "render", "embedding", "vector_write", "bm25", "final_validation", "total"} <= set(
        read_json(scenario.config.state_path())["sync_metrics"]["seconds"])
    assert runtime.read_status(scenario.config)["sync_progress"]["status"] == "ready"
    assert not (scenario.config.vault_root).exists()


def test_next_release_reuses_vectors_but_updates_provenance_and_deletes(scenario):
    first = sync(scenario)
    scenario.calls.clear()
    scenario.release("r2", ["alpha requirement", "beta revised", "gamma requirement", "delta requirement"])
    result = sync(scenario)
    assert scenario.calls == [["beta revised"]]
    assert result["index_generation"] != first["index_generation"]
    assert result["sync_metrics"]["cached_documents"] == 3
    store = ChromaStore(scenario.config.with_generation(result["index_generation"]), read_only=True)
    assert store.count() == 4
    assert {m["release_id"] for m in store.collection.get(include=["metadatas"])["metadatas"]} == {"r2"}
    assert ChromaStore(scenario.config.with_generation(first["index_generation"]), read_only=True).count() == 5


def test_query_and_batch_settings_do_not_rebuild_or_block_recall(scenario, monkeypatch):
    initial = sync(scenario)
    config = replace(scenario.config, use_reranker=True, reranker_model="new-reranker",
                     reranker_revision="3" * 40, top_k=8, index_batch_size=1)
    assert config.index_config_fingerprint() == scenario.config.index_config_fingerprint()
    assert config.query_config_fingerprint() != scenario.config.query_config_fingerprint()
    scenario.calls.clear()
    result = sync(scenario, config)
    assert result["updated_documents"] == [] and scenario.calls == []
    assert result["index_generation"] == initial["index_generation"]
    monkeypatch.setattr("qmd_like_rag.retriever.HybridRetriever.search", lambda *a, **k: [])
    response = runtime.recall(config, "requirement")
    assert response["warnings"] == []
    assert response["query_configuration_fingerprint"] == config.query_config_fingerprint()


def test_embedding_revision_change_invalidates_cache_and_index(scenario):
    first = sync(scenario)
    scenario.calls.clear()
    config = replace(scenario.config, embedding_revision="4" * 40)
    result = sync(scenario, config)
    assert sum(map(len, scenario.calls)) == 5
    assert result["index_generation"] != first["index_generation"]
    assert "index-configuration-mismatch" in runtime.recall(scenario.config, "test")["warnings"]


def test_name_only_remote_does_not_reuse_cross_release_vectors(scenario):
    config = replace(scenario.config, embedding_revision=None, model_audit_mode="name-only")
    sync(scenario, config)
    scenario.calls.clear()
    scenario.release("r2", ["alpha requirement", "beta requirement"])
    result = sync(scenario, config)
    assert result["sync_metrics"]["embedding_cache_enabled"] is False
    assert sum(map(len, scenario.calls)) == 2


def test_failed_sync_resumes_only_verified_batches(scenario):
    scenario.fail_at = 2
    with pytest.raises(RuntimeError, match="isolated embedding failure"):
        sync(scenario)
    assert not scenario.config.state_path().exists()
    assert runtime.read_status(scenario.config)["sync_progress"]["status"] == "failed"
    scenario.calls.clear()
    scenario.fail_at = None
    result = sync(scenario)
    assert result["sync_metrics"]["resumed_documents"] == 2
    assert sum(map(len, scenario.calls)) == 3
    assert result["document_count"] == 5


def test_corrupt_saved_batch_is_repaired_without_trusting_checkpoint(scenario):
    scenario.fail_at = 2
    with pytest.raises(RuntimeError):
        sync(scenario)
    progress = read_json(scenario.config.progress_path())
    store = ChromaStore(scenario.config.with_generation(progress["index_generation"]))
    first = store.collection.get()["ids"][0]
    store.collection.update(ids=[first], documents=["tampered text"], embeddings=[[9., 1., 2.]])
    scenario.fail_at = None
    result = sync(scenario)
    assert result["sync_metrics"]["resumed_documents"] == 0
    assert result["sync_metrics"]["cached_documents"] == 2
    assert "tampered text" not in store.collection.get(include=["documents"])["documents"]


def test_failed_new_release_and_rebuild_preserve_selected_generation(scenario, monkeypatch):
    original = sync(scenario)
    selected = scenario.config.state_path().read_bytes()
    from qmd_like_rag.storage.bm25_store import BM25Store
    monkeypatch.setattr(BM25Store, "save", lambda *a: (_ for _ in ()).throw(RuntimeError("isolated save failure")))
    with pytest.raises(RuntimeError, match="isolated save failure"):
        sync(scenario, rebuild=True)
    assert scenario.config.state_path().read_bytes() == selected
    assert read_json(scenario.config.progress_path())["index_generation"] != original["index_generation"]
    assert ChromaStore(scenario.config.with_generation(original["index_generation"]), read_only=True).count() == 5


def test_changed_release_during_build_is_not_published(scenario, monkeypatch):
    original_load = indexing.load_release_corpus
    calls = 0

    def load(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            scenario.release("r2", ["changed requirement"])
        return original_load(root)

    monkeypatch.setattr(indexing, "load_release_corpus", load)
    with pytest.raises(RuntimeError, match="changed during sync"):
        sync(scenario)
    assert not scenario.config.state_path().exists()


def test_cache_rejects_tampering_and_namespace_changes(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = EmbeddingCache(path, "identity-a", 3)
    cache.put(["text"], [[1., 2., 3.]])
    assert cache.get("text") == [1., 2., 3.]
    cache.connection.execute("UPDATE vectors SET payload='[9,9,9]'")
    cache.connection.commit()
    assert cache.get("text") is None
    cache.close()
    other = EmbeddingCache(path, "identity-b", 3)
    assert other.get("text") is None
    with pytest.raises(RuntimeError, match="dimension"):
        other.put(["text"], [[1., 2.]])
    other.close()


def test_token_and_row_budgets_and_no_silent_truncation():
    rows = [{"token_count": n} for n in (4, 4, 3, 5)]
    assert [len(batch) for batch in bounded_batches(rows, 3, 8)] == [2, 2]
    with pytest.raises(RuntimeError, match="exceeds embedding_batch_max_tokens"):
        list(bounded_batches(rows, 3, 3))


def test_writer_lock_excludes_another_process_and_status_stays_read_only(scenario):
    code = "from pathlib import Path; from qmd_like_rag.sync_support import writer_lock; " \
           "import sys; lock=writer_lock(Path(sys.argv[1])); lock.__enter__()"
    environment = {**os.environ, "PYTHONPATH": str(Path(indexing.__file__).resolve().parents[1])}
    with writer_lock(scenario.config.persist_dir):
        write_atomic(scenario.config.progress_path(), {"status": "building", "phase": "embedding"})
        outcome = subprocess.run([sys.executable, "-c", code, str(scenario.config.persist_dir)],
                                 capture_output=True, text=True, env=environment, timeout=10)
        assert outcome.returncode != 0 and "already running" in outcome.stderr
        assert runtime.read_status(scenario.config)["sync_progress"]["writer_active"] is True
    before = scenario.config.progress_path().read_bytes()
    assert runtime.read_status(scenario.config)["sync_progress"]["status"] == "interrupted"
    assert scenario.config.progress_path().read_bytes() == before


def test_http_status_responds_while_sync_is_waiting(scenario, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_sync(config, *args, **kwargs):
        with writer_lock(config.persist_dir):
            write_atomic(config.progress_path(), {"status": "building", "phase": "embedding"})
            entered.set()
            assert release.wait(5)
        return {"status": "ready"}

    monkeypatch.setattr(service, "sync", slow_sync)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(scenario.config))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    errors = []

    def submit():
        try:
            with urlopen(Request(base + "/sync", data=b"{}", headers={"Content-Type": "application/json"}),
                         timeout=5) as response:
                assert response.status == 200
        except Exception as exc:
            errors.append(exc)

    syncing = threading.Thread(target=submit)
    try:
        syncing.start()
        assert entered.wait(3)
        with urlopen(base + "/status", timeout=2) as response:
            assert json.load(response)["sync_progress"]["writer_active"] is True
        assert not release.is_set()
    finally:
        release.set()
        syncing.join(5)
        server.shutdown()
        server.server_close()
        serving.join(3)
    assert not errors
