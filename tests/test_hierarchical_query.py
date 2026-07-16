import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "build_section_query_index.py"
LOCATE = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "locate_source_sections.py"
TRACE = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "manage_query_trace.py"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    bundle = vault / "10_Raw" / "converted" / "0712XFNPXTS02_document_bundle"
    bundle.mkdir(parents=True)
    (bundle / "document.md").write_text(
        "# 消防系统\n\n## 水喷雾管网\n喷头参数 K=60，动作温度为 68 摄氏度。\n",
        encoding="utf-8",
    )
    write_json(
        bundle / "manifest.json",
        {
            "schema_version": "2.0",
            "profile": "engineering",
            "source": {"filename": "0712XFNPXTS02.pdf", "path": "10_Raw/0712XFNPXTS02.pdf"},
            "document": {"path": "document.md"},
            "outline": {"path": "outline.json"},
        },
    )
    write_json(
        bundle / "outline.json",
        {
            "schema_version": "2.0",
            "sections": [
                {
                    "id": "root",
                    "title": "消防系统",
                    "level": 1,
                    "parent": None,
                    "path": ["root"],
                    "start_line": 1,
                    "end_line": 4,
                    "pages": [1],
                    "assets": [],
                    "quality": "pass",
                },
                {
                    "id": "spray",
                    "title": "水喷雾管网",
                    "level": 2,
                    "parent": "root",
                    "path": ["root", "spray"],
                    "start_line": 3,
                    "end_line": 4,
                    "pages": [1],
                    "assets": [],
                    "quality": "pass",
                },
            ],
        },
    )
    write_json(
        vault / "_system" / "reports" / "0712XFNPXTS02.section-ledger.json",
        {
            "bundle_id": "bundle-0712",
            "revision": 2,
            "bundle": {"path": str(bundle), "document_sha256": "doc-hash"},
            "sections": [
                {
                    "id": "root",
                    "status": "ingested",
                    "content_ranges": [{"start_line": 1, "end_line": 2}],
                    "content_sha256": "root-hash",
                    "outputs": ["_system/reports/0712XFNPXTS02.spec-index.md"],
                },
                {
                    "id": "spray",
                    "status": "ingested",
                    "content_ranges": [{"start_line": 3, "end_line": 4}],
                    "content_sha256": "spray-hash",
                    "outputs": ["_system/reports/0712XFNPXTS02.spec-index.md"],
                },
            ],
        },
    )
    return vault


def test_build_projection_is_additive_and_has_no_summary(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(BUILD), str(vault)], capture_output=True, text=True, check=True
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    projection_path = vault / result["projections"][0]
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    assert projection["authority"] == "non-authoritative"
    assert projection["design_origin"] == "hanyu"
    assert projection["sections"][1]["path_titles"] == ["消防系统", "水喷雾管网"]
    assert "summary" not in projection["sections"][1]
    assert (vault / "10_Raw" / "converted" / "0712XFNPXTS02_document_bundle" / "outline.json").is_file()


def test_locator_scans_owned_content_and_returns_navigation_only(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    completed = subprocess.run(
        [sys.executable, str(LOCATE), str(vault), "水喷雾喷头 K=60", "--top-sections", "5"],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["authority"] == "candidate-navigation-only"
    assert result["design_origin"] == "hanyu"
    assert result["candidates"][0]["section_id"] == "spray"
    assert "60" in result["candidates"][0]["matched_terms"]["content"]


def test_query_trace_is_incremental_obsidian_readable_and_non_authoritative(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    started = subprocess.run(
        [
            sys.executable,
            str(TRACE),
            "start",
            str(vault),
            "水喷雾喷头 K=60 应如何核查？",
            "--session-id",
            "session-123",
            "--query-type",
            "evidence",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(started.stdout)["trace_id"]
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    subprocess.run(
        [
            sys.executable,
            str(LOCATE),
            str(vault),
            "水喷雾喷头 K=60",
            "--trace-id",
            trace_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(TRACE),
            "event",
            str(vault),
            trace_id,
            "--stage",
            "source-verification",
            "--route",
            "converted-source",
            "--hit-count",
            "1",
            "--accepted-count",
            "1",
            "--accepted-path",
            "10_Raw/converted/0712XFNPXTS02_document_bundle/document.md",
            "--summary",
            "Verified the complete owned section.",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(TRACE),
            "finish",
            str(vault),
            trace_id,
            "--evidence-level",
            "source-backed",
            "--conclusion",
            "The checked section supports K=60.",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    trace_root = vault / "_system" / "reports" / "query-traces"
    state = json.loads((trace_root / "_data" / f"{trace_id}.query-trace.json").read_text(encoding="utf-8"))
    note = (trace_root / f"{trace_id}.query-trace.md").read_text(encoding="utf-8")
    dashboard = (trace_root / "Query Trace Dashboard.md").read_text(encoding="utf-8")
    assert state["authority"] == "non-authoritative-runtime-log"
    assert state["status"] == "completed"
    assert state["events"][0]["route"] == "hierarchical-search"
    assert state["events"][0]["candidates"][0]["section_id"] == "spray"
    assert "hierarchical_search_used: true" in note
    assert "Runtime trace, not evidence" in note
    assert "session-123" in note
    assert "```dataview" in dashboard


def test_trace_failure_does_not_block_hierarchical_retrieval(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(LOCATE),
            str(vault),
            "水喷雾喷头 K=60",
            "--trace-id",
            "missing-trace",
        ],
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert result["status"] == "ok"
    assert result["candidates"][0]["section_id"] == "spray"
    assert "query trace append failed" in completed.stderr
