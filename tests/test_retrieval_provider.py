from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY_ADAPTER = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "retrieve_candidates.py"
FINALIZE_ADAPTER = ROOT / "hermes-obsidian-knowledge-finalize" / "scripts" / "sync_release_index.py"
QUERY_SKILL = ROOT / "hermes-obsidian-controlled-query" / "SKILL.md"
FINALIZE_SKILL = ROOT / "hermes-obsidian-knowledge-finalize" / "SKILL.md"
QUERY_CONFIG = ROOT / "hermes-obsidian-controlled-query" / "config" / "retrieval-provider.json"
FINALIZE_CONFIG = ROOT / "hermes-obsidian-knowledge-finalize" / "config" / "retrieval-provider.json"


def write_fake_provider(path: Path) -> None:
    path.write_text(
        """import argparse, hashlib, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('--vault-root', required=True)
p.add_argument('--query')
p.add_argument('--top-k')
p.add_argument('--rebuild', action='store_true')
p.add_argument('--release-id')
p.add_argument('--release-hash')
args = p.parse_args()
vault = Path(args.vault_root)
source = vault / '10_Raw' / 'converted' / 'example' / 'document.md'
digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ''
release_path = vault / '_system' / 'knowledge-releases' / 'release-test' / 'manifest.json'
release_hash = hashlib.sha256(release_path.read_bytes()).hexdigest()
base = {
  'protocol_version': 'hermes-coarse-recall/v1',
  'provider': 'qmd-like-rag',
  'provider_version': 'test',
  'vault_id': 'test-vault',
  'release_id': 'release-test',
  'release_hash': release_hash,
  'index_generation': 'generation-test',
  'capabilities': {'source_units': True, 'release_driven': True, 'projection_kinds':['source_unit','knowledge_page']},
}
if args.command == 'recall':
  base.update({'status':'ok','authority':'candidate-navigation-only','index_fingerprint':'idx','warnings':[],
    'candidates':[{'vault_path':'10_Raw/converted/example/document.md','line_start':2,'line_end':3,
      'source_sha256':digest,'snippet':'供水','projection_kind':'knowledge_page',
      'projection_fingerprint':'sha256:projection','release_id':'release-test','release_hash':release_hash,
      'page_id':'page-test','page_revision_id':'revision-test','source_unit_refs':[]} ]})
else:
  if args.release_id != 'release-test' or args.release_hash != release_hash:
    raise SystemExit(4)
  base.update({'status':'ready','configuration':{'renderer_version':'source-unit-renderer/v1'},
    'configuration_fingerprint':'cfg','model_fingerprint':'model',
    'models':{'embedding':{'identity':'BAAI/bge-m3','revision':'0'*40,'dimension':1024},'reranker':None},
    'tokenizer':{'identity':'BAAI/bge-m3','sha256':'abc','ready':True,'max_tokens':8192},
    'renderer_version':'source-unit-renderer/v1','renderer_fingerprint':'renderer',
    'corpus_fingerprint':'corpus','index_fingerprint':'idx','document_count':1,'chunk_count':1,
    'projection_counts':{'source_unit':0,'knowledge_page':1},'errors':[]})
print(json.dumps(base, ensure_ascii=False))
""",
        encoding="utf-8",
    )


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    source = vault / "10_Raw" / "converted" / "example" / "document.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 系统\n供水系统应保持可用。\n验证原文。\n", encoding="utf-8")
    release = {
        "contract": "hermes-knowledge-release/v1", "release_id": "release-test",
        "state": "completed", "index_eligibility": [{
            "kind": "knowledge_page", "id": "page-test", "eligible": True, "reasons": [],
            "resource_id": None, "unit_set_id": None, "page_revision_id": "revision-test",
            "source_unit_refs": [],
        }],
    }
    release_path = vault / "_system" / "knowledge-releases" / "release-test" / "manifest.json"
    release_path.parent.mkdir(parents=True)
    release_path.write_text(json.dumps(release, sort_keys=True), encoding="utf-8")
    state_path = vault / "_system" / "metadata" / "knowledge-release-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"current_release_id": "release-test"}), encoding="utf-8")
    return vault


def config_for(tmp_path: Path, provider: Path) -> Path:
    config = tmp_path / "provider.json"
    config.write_text(
        json.dumps(
            {
                "provider": "qmd-like-rag",
                "transport": "command",
                "command": [sys.executable, str(provider)],
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    return config


def test_query_adapter_normalizes_provider_output(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    provider = tmp_path / "provider.py"
    write_fake_provider(provider)
    completed = subprocess.run(
        [sys.executable, str(QUERY_ADAPTER), str(vault), "供水可用性", "--provider-config", str(config_for(tmp_path, provider))],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["authority"] == "candidate-navigation-only"
    assert result["candidates"][0]["vault_path"] == "10_Raw/converted/example/document.md"
    assert result["candidates"][0]["source_hash_matches"] is True
    assert result["candidates"][0]["retrieval_routes"] == ["qmd-like-rag"]


def test_default_provider_configs_enable_read_only_query_but_not_sync(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    query = subprocess.run(
        [sys.executable, str(QUERY_ADAPTER), str(vault), "供电可用性"],
        capture_output=True,
        text=True,
        check=True,
    )
    query_result = json.loads(query.stdout)
    assert json.loads(QUERY_CONFIG.read_text(encoding="utf-8"))["enabled"] is True
    assert json.loads(FINALIZE_CONFIG.read_text(encoding="utf-8"))["enabled"] is False
    assert json.loads(QUERY_CONFIG.read_text(encoding="utf-8"))["provider_config"] == "/root/.config/qmd-like-rag/main.json"
    assert json.loads(FINALIZE_CONFIG.read_text(encoding="utf-8"))["provider_config"] == "/root/.config/qmd-like-rag/main.json"
    assert query_result["status"] in {"ok", "unavailable"}
    assert query_result["candidates"] == []

    sync = subprocess.run(
        [sys.executable, str(FINALIZE_ADAPTER), str(vault), "--no-write-manifest"],
        capture_output=True,
        text=True,
        check=False,
    )
    sync_result = json.loads(sync.stdout)
    assert sync.returncode == 2
    assert sync_result["status"] == "warn"
    assert sync_result["index"]["status"] == "disabled"
    assert sync_result["index"]["errors"] == []


def test_disabled_adapters_do_not_require_provider_runtime_or_models(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    config = tmp_path / "disabled-provider.json"
    config.write_text(
        json.dumps(
            {
                "provider": "qmd-like-rag",
                "enabled": False,
                "transport": "command",
                "command": [str(tmp_path / "provider-is-not-installed")],
                "provider_config": str(tmp_path / "models-are-not-installed.json"),
            }
        ),
        encoding="utf-8",
    )

    query = subprocess.run(
        [
            sys.executable,
            str(QUERY_ADAPTER),
            str(vault),
            "供电可用性",
            "--provider-config",
            str(config),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    query_result = json.loads(query.stdout)
    assert query_result["status"] == "disabled"
    assert query_result["candidates"] == []
    assert query_result["warnings"] == []

    sync = subprocess.run(
        [
            sys.executable,
            str(FINALIZE_ADAPTER),
            str(vault),
            "--provider-config",
            str(config),
            "--no-write-manifest",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sync_result = json.loads(sync.stdout)
    assert sync.returncode == 2
    assert sync_result["index"]["status"] == "disabled"
    assert sync_result["index"]["errors"] == []


def test_finalize_adapter_writes_portable_manifest(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    provider = tmp_path / "provider.py"
    write_fake_provider(provider)
    completed = subprocess.run(
        [sys.executable, str(FINALIZE_ADAPTER), str(vault), "--provider-config", str(config_for(tmp_path, provider))],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    manifest_path = vault / "_system" / "reports" / "retrieval-index-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert manifest["status"] == "ready"
    assert manifest["provider_version"] == "test"
    assert manifest["configuration"]["renderer_version"] == "source-unit-renderer/v1"
    assert manifest["release_id"] == "release-test"
    assert manifest["index_generation"] == "generation-test"
    assert manifest["tokenizer"]["ready"] is True
    assert manifest["models"]["embedding"]["dimension"] == 1024
    assert manifest["last_success"]
    serialized = json.dumps(manifest)
    assert str(tmp_path) not in serialized
    assert "base_url" not in serialized


def test_skills_keep_provider_as_navigation_and_finalize_only_writer() -> None:
    query = QUERY_SKILL.read_text(encoding="utf-8")
    finalize = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "optional coarse recall || hierarchical routing" in query
    assert "retrieve_query_scope.py" in query
    assert "governed-layer-first traditional search" in query
    assert "Query must never run Provider `sync`" in query
    assert "extraction QA labels are verification metadata, not relevance boosts or penalties" in query
    assert "sync_release_index.py" in finalize
    assert "only Skill-side command that may update" in finalize
