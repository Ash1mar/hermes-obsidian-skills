from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY_ADAPTER = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "retrieve_candidates.py"
INGEST_ADAPTER = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "sync_retrieval_index.py"
QUERY_SKILL = ROOT / "hermes-obsidian-controlled-query" / "SKILL.md"
INGEST_SKILL = ROOT / "hermes-obsidian-controlled-ingest" / "SKILL.md"
QUERY_CONFIG = ROOT / "hermes-obsidian-controlled-query" / "config" / "retrieval-provider.json"
INGEST_CONFIG = ROOT / "hermes-obsidian-controlled-ingest" / "config" / "retrieval-provider.json"


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
args = p.parse_args()
vault = Path(args.vault_root)
source = vault / '10_Raw' / 'converted' / 'example' / 'document.md'
digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ''
base = {
  'protocol_version': 'hermes-coarse-recall/v1',
  'provider': 'qmd-like-rag',
  'provider_version': 'test',
  'vault_id': 'test-vault',
}
if args.command == 'recall':
  base.update({'status':'ok','authority':'candidate-navigation-only','index_fingerprint':'idx','warnings':[],
    'candidates':[{'vault_path':'10_Raw/converted/example/document.md','line_start':2,'line_end':3,'source_sha256':digest,'snippet':'供水'}]})
else:
  base.update({'status':'ready','configuration':{'chunk_size':800,'include_patterns':['30_Cards/**/*.md']},'configuration_fingerprint':'cfg','model_fingerprint':'model','models':{'embedding':{'identity':'BAAI/bge-m3','revision':'0'*40,'dimension':1024},'reranker':None},'corpus_fingerprint':'corpus','index_fingerprint':'idx','document_count':1,'chunk_count':2,'errors':[]})
print(json.dumps(base, ensure_ascii=False))
""",
        encoding="utf-8",
    )


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    source = vault / "10_Raw" / "converted" / "example" / "document.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 系统\n供水系统应保持可用。\n验证原文。\n", encoding="utf-8")
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


def test_default_provider_configs_disable_query_and_sync_until_deployed(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    query = subprocess.run(
        [sys.executable, str(QUERY_ADAPTER), str(vault), "供电可用性"],
        capture_output=True,
        text=True,
        check=True,
    )
    query_result = json.loads(query.stdout)
    query_config = json.loads(QUERY_CONFIG.read_text(encoding="utf-8"))
    ingest_config = json.loads(INGEST_CONFIG.read_text(encoding="utf-8"))
    assert query_config["enabled"] is False
    assert ingest_config["enabled"] is False
    assert query_config["transport"] == "http"
    assert ingest_config["transport"] == "http"
    assert query_config["base_url"] == "http://qmd-like-rag:8781"
    assert ingest_config["base_url"] == "http://qmd-like-rag:8781"
    assert query_result["status"] == "disabled"
    assert query_result["candidates"] == []

    sync = subprocess.run(
        [sys.executable, str(INGEST_ADAPTER), str(vault), "--no-write-manifest"],
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
            str(INGEST_ADAPTER),
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


def test_ingest_adapter_writes_portable_manifest(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    provider = tmp_path / "provider.py"
    write_fake_provider(provider)
    completed = subprocess.run(
        [sys.executable, str(INGEST_ADAPTER), str(vault), "--provider-config", str(config_for(tmp_path, provider))],
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
    assert manifest["configuration"]["chunk_size"] == 800
    assert manifest["models"]["embedding"]["dimension"] == 1024
    assert manifest["last_success"]
    serialized = json.dumps(manifest)
    assert str(tmp_path) not in serialized
    assert "base_url" not in serialized


def test_skills_keep_provider_as_navigation_and_ingest_only_writer() -> None:
    query = QUERY_SKILL.read_text(encoding="utf-8")
    ingest = INGEST_SKILL.read_text(encoding="utf-8")
    assert "optional coarse recall || hierarchical routing" in query
    assert "retrieve_query_scope.py" in query
    assert "governed-layer-first traditional search" in query
    assert "Query must never run Provider `sync`" in query
    assert "extraction QA labels are verification metadata, not relevance boosts or penalties" in query
    assert "sync_retrieval_index.py" in ingest
    assert "only Skill-side path that may update" in ingest
