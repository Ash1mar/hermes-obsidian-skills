import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "hermes-obsidian-controlled-ingest" / "scripts" / "build_section_query_index.py"
LOCATE = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "locate_source_sections.py"
SCOPE = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "retrieve_query_scope.py"
TRACE = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "manage_query_trace.py"
SESSION = ROOT / "hermes-obsidian-controlled-query" / "scripts" / "query_session.py"
QUERY_SKILL = ROOT / "hermes-obsidian-controlled-query" / "SKILL.md"
INGEST_SKILL = ROOT / "hermes-obsidian-controlled-ingest" / "SKILL.md"
TRACE_REFERENCE = ROOT / "hermes-obsidian-controlled-query" / "references" / "query-tracing.md"
ANSWER_FORMAT = ROOT / "hermes-obsidian-controlled-query" / "references" / "answer-format.md"
EVIDENCE_LEVELS = ROOT / "hermes-obsidian-controlled-query" / "references" / "evidence-levels.md"
QUERY_WORKFLOW = ROOT / "hermes-obsidian-controlled-query" / "references" / "query-workflow.md"


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
    (bundle / "tables").mkdir()
    (bundle / "tables" / "table_spray.md").write_text(
        "# 喷头参数表\n\n<!-- table-id: table_spray; source-page: 1 -->\n\n| K | 温度 |\n| --- | --- |\n| 60 | 68 ℃ |\n",
        encoding="utf-8",
    )
    (bundle / "tables" / "table_spray_source.jpg").write_bytes(b"fixture-image")
    source_pdf = vault / "10_Raw" / "0712XFNPXTS02.pdf"
    source_pdf.parent.mkdir(parents=True, exist_ok=True)
    source_pdf.write_bytes(b"%PDF-1.4 fixture")
    write_json(
        bundle / "manifest.json",
        {
            "schema_version": "2.0",
            "profile": "engineering",
            "source": {"filename": "0712XFNPXTS02.pdf", "path": "10_Raw/0712XFNPXTS02.pdf"},
            "document": {"path": "document.md"},
            "outline": {"path": "outline.json"},
            "tables": [
                {
                    "id": "table_spray",
                    "caption": "喷头参数表",
                    "page_start": 1,
                    "page_end": 1,
                    "path": "tables/table_spray.md",
                    "evidence_path": "tables/table_spray_source.jpg",
                    "bbox": [1, 2, 3, 4],
                    "quality": "pass",
                    "section_id": "spray",
                }
            ],
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
                    "assets": ["table_spray"],
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
    source_map = vault / "_system" / "reports" / "0712XFNPXTS02.source-map.md"
    source_map.write_text(
        "---\ntype: source-map\nvalidation_status: pass\nledger_revision: 2\n---\n",
        encoding="utf-8",
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


def test_scope_retrieval_survives_missing_provider_and_keeps_hierarchical_results(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    missing_config = tmp_path / "missing-provider.json"
    write_json(
        missing_config,
        {"provider": "qmd-like-rag", "transport": "command", "command": ["missing-qmd-like-rag"]},
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SCOPE),
            str(vault),
            "水喷雾喷头 K=60",
            "--provider-config",
            str(missing_config),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["routes"]["coarse_recall"]["status"] == "unavailable"
    assert result["routes"]["hierarchical_search"]["hit_count"] >= 1
    assert result["candidates"][0]["section_id"] == "spray"
    assert result["candidates"][0]["retrieval_routes"] == ["hierarchical-search"]
    assert result["duration_ms"] >= 0


def test_scope_fusion_expands_provider_chunk_and_records_duplicate_reason(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    started = subprocess.run(
        [sys.executable, str(TRACE), "start", str(vault), "水喷雾喷头 K=60"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(started.stdout)["trace_id"]
    provider = tmp_path / "provider.py"
    provider.write_text(
        """import argparse, json
p=argparse.ArgumentParser(); p.add_argument('command'); p.add_argument('--vault-root'); p.add_argument('--query'); p.add_argument('--top-k'); a=p.parse_args()
print(json.dumps({'protocol_version':'hermes-coarse-recall/v1','provider':'qmd-like-rag','provider_version':'test','status':'ok','authority':'candidate-navigation-only','index_fingerprint':'idx','warnings':[],'candidates':[{'vault_path':'10_Raw/converted/0712XFNPXTS02_document_bundle/document.md','line_start':3,'line_end':4,'heading':'水喷雾管网','score':0.9}]}))
""",
        encoding="utf-8",
    )
    provider_config = tmp_path / "provider.json"
    write_json(
        provider_config,
        {
            "provider": "qmd-like-rag",
            "transport": "command",
            "command": [sys.executable, str(provider)],
            "timeout_seconds": 10,
        },
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SCOPE),
            str(vault),
            "水喷雾喷头 K=60",
            "--provider-config",
            str(provider_config),
            "--trace-id",
            trace_id,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    spray = next(item for item in result["candidates"] if item["section_id"] == "spray")
    assert spray["retrieval_routes"] == ["hierarchical-search", "qmd-like-rag"]
    assert set(spray["route_scores"]) == {"hierarchical-search", "qmd-like-rag"}
    assert spray["fusion_score"] > 0
    assert spray["rerank_score"] == spray["fusion_score"]
    assert result["fusion"]["duration_ms"] >= 0
    assert result["fusion"]["eliminated_count"] >= 1
    assert any(item["reason"] == "duplicate-section-merged" for item in result["rejected"])
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert [event["stage"] for event in state["events"]] == [
        "coarse-recall",
        "hierarchical-candidate-location",
        "candidate-fusion",
    ]
    assert state["events"][2]["duration_ms"] >= 0
    assert state["events"][2]["rejected"][0]["reason"] == "duplicate-section-merged"


def test_locator_compacts_overlapping_query_ngrams(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    completed = subprocess.run(
        [sys.executable, str(LOCATE), str(vault), "消防系统水喷雾喷头参数"],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert len(result["terms"]) <= 12
    for values in result["candidates"][0]["matched_terms"].values():
        assert len(values) <= 8
        assert not any(left != right and left in right for left in values for right in values)


def test_query_session_completes_explicit_visual_verification_policy(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)

    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾喷头 K=60 的参数是什么？",
            "--session-id",
            "session-fast",
            "--query-type",
            "evidence",
            "--verification-required",
            "--request-id",
            "req-fast",
            "--question-index",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    begin_result = json.loads(begun.stdout)
    trace_id = begin_result["trace"]["trace_id"]
    assert len(begin_result["scope"]["candidates"]) <= 5
    assert begin_result["scope"]["candidates"][0]["section_id"] == "spray"

    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    inspect_result = json.loads(inspected.stdout)
    packet = inspect_result["evidence_packets"][0]
    assert "K=60" in packet["content"]
    assert packet["source_exists"] is True
    assert packet["source_path"] == "10_Raw/0712XFNPXTS02.pdf"
    assert packet["assets"][0]["id"] == "table_spray"
    assert "| 60 | 68 ℃ |" in packet["assets"][0]["content"]
    assert packet["qa"]["source_map_validation_status"] == "pass"
    assert packet["verification"]["status"] == "ready"
    assert packet["evidence_ref"] == "P1"
    prepared = subprocess.run(
        [sys.executable, str(SESSION), "verify", str(vault), trace_id, "--evidence-ref", "P1"],
        capture_output=True,
        text=True,
        check=True,
    )
    carrier = json.loads(prepared.stdout)["verification"][0]["paths"][0]

    decision = {
        "status": "completed",
        "evidence_level": "clear",
        "claims": [
            {
                "text": "The checked section supports K=60.",
                "status": "supported",
                "evidence_refs": ["P1"],
            },
            {
                "text": "The checked table supports 68 ℃.",
                "status": "supported",
                "evidence_refs": ["P1"],
            },
        ],
        "verified_evidence_refs": ["P1"],
        "events": [
            {
                "stage": "page-asset-verification",
                "route": "original-pdf",
                "status": "completed",
                "summary": "Checked page 1 and the table evidence image.",
                "evidence_refs": ["P1"],
                "inspected_paths": [carrier],
            }
        ],
        "conclusion": "The checked section supports K=60.",
        "unresolved": [],
    }
    finalized = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(decision, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    final_result = json.loads(finalized.stdout)
    assert final_result["status"] == "completed"
    assert final_result["trace_verified"] is True
    state_path = Path(final_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["metrics"]["command_count"] == 4
    assert state["evidence"][0]["evidence_id"] == "E1"
    assert state["evidence"][0]["document_version"] == packet["document_version"]
    assert state["claims"][0]["claim_id"] == "C1"
    assert state["answer_capsule"]["claims"][0]["text"] == "The checked section supports K=60."
    assert len(state["answer_capsule"]["sources"]) == 1
    assert state["answer_capsule"]["claims"][0]["source_ids"] == ["S1"]
    assert state["answer_capsule"]["claims"][1]["source_ids"] == ["S1"]
    stages = {event["stage"] for event in state["events"]}
    assert {
        "candidate-review",
        "document-reading",
        "table-figure-resolution",
        "provenance-resolution",
        "answer-synthesis",
        "claim-evidence-mapping",
        "page-asset-verification",
    } <= stages
    note = Path(final_result["note_path"]).read_text(encoding="utf-8")
    assert "Query-session duration:" in note
    assert "Accounted stage duration:" in note
    assert "Recorded:" in note
    assert "qmd-like-rag" in note
    assert 'effective_routes:\n  - "qmd-like-rag"' not in note
    summarized = subprocess.run(
        [sys.executable, str(SESSION), "request-summary", str(vault), "req-fast"],
        capture_output=True,
        text=True,
        check=True,
    )
    request_result = json.loads(summarized.stdout)
    assert request_result["question_count"] == 1
    assert "The checked section supports K=60." in request_result["answer_markdown"]
    assert request_result["metrics"]["controlled_request_duration_ms"] >= 0
    assert request_result["metrics"]["measurement_boundary"] == "first query-session begin through last finalized trace"


def test_query_session_rejects_multiple_questions_before_trace_creation(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    question = "喷水强度是多少？通信接口应注意什么？喷头审查关注什么？"
    rejected = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), question],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "multiple questions detected before trace creation" in rejected.stderr
    data_root = vault / "_system" / "reports" / "query-traces" / "_data"
    assert not data_root.exists() or not list(data_root.glob("*.query-trace.json"))

    numbered = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), "1. 查询参数\n2. 查询接口"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert numbered.returncode != 0
    assert "multiple questions detected before trace creation" in numbered.stderr

    missing_reason = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), question, "--coupled"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_reason.returncode != 0
    assert "--coupled requires a non-empty --coupled-reason" in missing_reason.stderr

    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    allowed = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            question,
            "--coupled",
            "--coupled-reason",
            "All subparts require the same source section and evidence set.",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(allowed.stdout)["trace"]["trace_id"]
    state_path = data_root / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["workflow_state"]["question_shape"]["multiple_detected"] is True
    assert state["workflow_state"]["coupled_question"] is True
    assert state["workflow_state"]["coupled_reason"].startswith("All subparts")


def test_query_session_rejects_empty_claim_then_accepts_text_alias(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), "水喷雾参数"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence_ref = json.loads(inspected.stdout)["evidence_packets"][0]["evidence_ref"]
    empty_decision = {
        "evidence_level": "source-backed",
        "claims": [{"evidence_refs": [evidence_ref]}],
    }
    rejected = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(empty_decision),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "decision claim 1 text must not be empty" in rejected.stderr
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "in_progress"
    assert state["evidence"] == []
    assert state["claims"] == []

    unknown_field = {
        "evidence_level": "needs-qa",
        "claims": [{"text": "A claim.", "evidence_refs": [evidence_ref]}],
        "unresolved": ["Verification pending."],
        "unresolved_itemz": [],
    }
    rejected_unknown = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(unknown_field),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected_unknown.returncode != 0
    assert "unsupported fields: unresolved_itemz" in rejected_unknown.stderr

    repaired_decision = {
        "evidence_level": "needs-qa",
        "claims": [{"statement": "The inspected source supports the water-spray parameter.", "evidence_refs": [evidence_ref]}],
        "events": [{"type": "inspection", "summary": "Recorded by a newer model event vocabulary."}],
        "unresolved_items": ["Original-page verification was not completed."],
    }
    finalized = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(repaired_decision),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    final_state = json.loads(Path(json.loads(finalized.stdout)["state_path"]).read_text(encoding="utf-8"))
    assert final_state["claims"][0]["text"] == "The inspected source supports the water-spray parameter."
    assert final_state["answer_capsule"]["claims"][0]["text"] == final_state["claims"][0]["text"]
    assert final_state["unresolved"] == ["Original-page verification was not completed."]
    assert final_state["answer_capsule"]["sources"][0]["source_id"] == "S1"
    assert final_state["answer_capsule"]["claims"][0]["source_ids"] == ["S1"]
    extension_event = next(event for event in final_state["events"] if event.get("extensions", {}).get("type"))
    assert extension_event["stage"] == "unspecified"
    assert extension_event["extensions"] == {"type": "inspection"}
    assert 'Extensions: `{"type": "inspection"}`' in Path(json.loads(finalized.stdout)["note_path"]).read_text(encoding="utf-8")


def test_query_session_finalize_is_atomic_on_invalid_claim(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), "水喷雾参数"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    invalid = {
        "status": "completed",
        "evidence_level": "clear",
        "evidence": [],
        "claims": [{"claim_id": "C1", "text": "bad", "status": "supported", "evidence_ids": ["missing"]}],
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--manifest-json",
            json.dumps(invalid),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "in_progress"
    assert state["evidence"] == []
    assert state["claims"] == []


def test_query_session_requires_inspect_after_supplement(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [sys.executable, str(SESSION), "begin", str(vault), "水喷雾参数"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    packet_ref = json.loads(inspected.stdout)["evidence_packets"][0]["evidence_ref"]
    subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "supplement",
            str(vault),
            trace_id,
            "K=60 表格",
            "--reason",
            "Confirm the exact table value.",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    decision = {
        "evidence_level": "source-backed",
        "claims": [{"text": "K=60 is present.", "evidence_refs": [packet_ref]}],
        "unresolved": [],
    }
    blocked = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(decision),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "must be followed by inspect" in blocked.stderr
    subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    finalized = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(decision),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    state = json.loads(Path(json.loads(finalized.stdout)["state_path"]).read_text(encoding="utf-8"))
    stages = [event["stage"] for event in state["events"]]
    assert "supplemental-retrieval" in stages
    assert "evidence-gap-review" in stages
    assert state["metrics"]["command_count"] == 5


def test_query_session_inherits_hermes_session_context(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_SESSION_ID": "real-hermes-session",
            "HERMES_SESSION_MESSAGE_ID": "message-42",
            "HERMES_SESSION_PLATFORM": "cli",
        }
    )
    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾参数",
            "--session-id",
            "model-supplied-session",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["session_id"] == "real-hermes-session"
    assert state["session_message_id"] == "message-42"
    assert state["session_platform"] == "cli"


def test_query_session_bootstrap_returns_exact_rules_and_capabilities(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    (tmp_path / "ENVIRONMENT.md").write_text("# Runtime\nUse the verified test runtime.\n", encoding="utf-8")
    (vault / "AGENTS.md").write_text("# Vault rules\nKeep raw evidence read-only.\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SESSION), "bootstrap", str(vault)],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    paths = {Path(item["path"]).name for item in result["required_rules"]}
    assert {"AGENTS.md", "ENVIRONMENT.md"} <= paths
    assert result["verification_runtime"]["policy"].startswith("one deterministic")
    config_root = QUERY_SKILL.parent / "config"
    expected_routing_path = next(
        path for path in (config_root / "domain-routing.json", config_root / "intranet.json") if path.is_file()
    )
    assert Path(result["routing_config_path"]).name == expected_routing_path.name
    assert result["routing"] == json.loads(expected_routing_path.read_text(encoding="utf-8"))
    assert result["next_command"] == "begin"


def test_query_session_does_not_infer_verification_policy_from_question_terms(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾喷头参数 K=60 表格 formula pressure 是多少？",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    inspect_result = json.loads(inspected.stdout)
    assert inspect_result["next_command"] == "finalize"
    assert "type" not in inspect_result["finalize_contract"]["event_standard_fields"]
    assert "Unknown event fields are preserved under extensions" in inspect_result["finalize_contract"]["event_extension_policy"]
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    workflow = json.loads(state_path.read_text(encoding="utf-8"))["workflow_state"]
    assert workflow["verification_required"] is False
    assert workflow["verification_requirement_reason"] == "not requested"


def test_query_session_accepts_exact_projected_section_outside_fused_candidates(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "K=60 水喷雾喷头参数",
            "--top-sections",
            "1",
            "--compact-limit",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(begun.stdout)
    trace_id = result["trace"]["trace_id"]
    assert all(candidate["section_id"] != "root" for candidate in result["scope"]["candidates"])
    document_path = "10_Raw/converted/0712XFNPXTS02_document_bundle/document.md"
    inspected = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "inspect",
            str(vault),
            trace_id,
            "--candidate",
            f"{document_path}::root",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    packet = json.loads(inspected.stdout)["evidence_packets"][0]
    assert packet["document_path"] == document_path
    assert packet["section_id"] == "root"
    assert "消防系统" in packet["content"]
    assert packet["selection_origin"] == "projection-exact"


def test_query_session_verify_uses_registered_carrier_once(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾喷头参数是多少？",
            "--verification-required",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence_ref = json.loads(inspected.stdout)["evidence_packets"][0]["evidence_ref"]
    prepared = subprocess.run(
        [sys.executable, str(SESSION), "verify", str(vault), trace_id, "--evidence-ref", evidence_ref],
        capture_output=True,
        text=True,
        check=True,
    )
    verification = json.loads(prepared.stdout)
    assert verification["verification"][0]["status"] == "ready"
    assert verification["verification"][0]["mode"] == "evidence-image"
    assert "pdftotext" in verification["stopping_rule"]
    carrier = verification["verification"][0]["paths"][0]
    decision = {
        "evidence_level": "clear",
        "claims": [{"text": "K=60 is confirmed.", "evidence_refs": [evidence_ref]}],
        "verified_evidence_refs": [evidence_ref],
        "events": [
            {
                "stage": "page-asset-verification",
                "status": "completed",
                "route": "evidence-image",
                "summary": "Visually checked the registered table image.",
                "evidence_refs": [evidence_ref],
                "inspected_paths": [carrier],
            }
        ],
        "unresolved": [],
    }
    finalized = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            trace_id,
            "--decision-json",
            json.dumps(decision),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    state = json.loads(Path(json.loads(finalized.stdout)["state_path"]).read_text(encoding="utf-8"))
    assert state["metrics"]["command_count"] == 4
    assert any(event["stage"] == "verification-readiness" for event in state["events"])


def test_query_session_verification_uses_viewer_or_fast_fails_without_carrier(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    bundle = vault / "10_Raw" / "converted" / "0712XFNPXTS02_document_bundle"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["tables"] = []
    write_json(bundle / "manifest.json", manifest)
    outline = json.loads((bundle / "outline.json").read_text(encoding="utf-8"))
    for section in outline["sections"]:
        section["assets"] = []
    write_json(bundle / "outline.json", outline)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    begun = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾参数是多少？",
            "--verification-required",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(begun.stdout)["trace"]["trace_id"]
    inspected = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), trace_id, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence_ref = json.loads(inspected.stdout)["evidence_packets"][0]["evidence_ref"]
    env = os.environ.copy()
    env["PATH"] = ""
    prepared = subprocess.run(
        [sys.executable, str(SESSION), "verify", str(vault), trace_id, "--evidence-ref", evidence_ref],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    result = json.loads(prepared.stdout)
    verification = result["verification"][0]
    if verification["status"] == "ready":
        assert verification["mode"] == "viewer"
        assert verification["viewer_url"]
    else:
        assert verification["status"] == "unavailable"
        assert verification["recommended_evidence_level"] == "needs-qa"
        assert verification["required_unresolved"]
    assert "Do not probe pdftotext" in result["stopping_rule"]


def test_query_session_blocks_overlapping_request_and_closes_with_capsules(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    subprocess.run([sys.executable, str(BUILD), str(vault)], check=True, capture_output=True, text=True)
    first = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾参数是多少？",
            "--request-id",
            "req-sequential",
            "--question-index",
            "1",
            "--question-count",
            "2",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    first_trace = json.loads(first.stdout)["trace"]["trace_id"]
    premature_summary = subprocess.run(
        [sys.executable, str(SESSION), "request-summary", str(vault), "req-sequential"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert premature_summary.returncode != 0
    assert "unfinished traces" in premature_summary.stderr
    blocked = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾接口要求是什么？",
            "--request-id",
            "req-sequential",
            "--question-index",
            "2",
            "--question-count",
            "2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "in-progress trace" in blocked.stderr
    first_inspect = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), first_trace, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    first_ref = json.loads(first_inspect.stdout)["evidence_packets"][0]["evidence_ref"]
    subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            first_trace,
            "--decision-json",
            json.dumps(
                {
                    "evidence_level": "source-backed",
                    "claims": [{"text": "The parameter is present.", "evidence_refs": [first_ref]}],
                    "unresolved": [],
                }
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    second = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "begin",
            str(vault),
            "水喷雾接口要求是什么？",
            "--request-id",
            "req-sequential",
            "--question-index",
            "2",
            "--question-count",
            "2",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    second_trace = json.loads(second.stdout)["trace"]["trace_id"]
    second_inspect = subprocess.run(
        [sys.executable, str(SESSION), "inspect", str(vault), second_trace, "--candidate", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    second_ref = json.loads(second_inspect.stdout)["evidence_packets"][0]["evidence_ref"]
    closed = subprocess.run(
        [
            sys.executable,
            str(SESSION),
            "finalize",
            str(vault),
            second_trace,
            "--decision-json",
            json.dumps(
                {
                    "evidence_level": "source-backed",
                    "claims": [{"text": "The interface requirement is present.", "evidence_refs": [second_ref]}],
                    "unresolved": [],
                }
            ),
            "--close-request",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    request = json.loads(closed.stdout)["request"]
    assert request["question_count"] == 2
    assert request["metrics"]["sequential"] is True
    assert request["metrics"]["overlap_count"] == 0


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
            "evidence",
            str(vault),
            trace_id,
            "--evidence-id",
            "E1",
            "--path",
            "10_Raw/converted/0712XFNPXTS02_document_bundle/document.md",
            "--document-version",
            "doc-hash",
            "--section-id",
            "spray",
            "--page",
            "1",
            "--block-id",
            "spray-lines-3-4",
            "--original-asset-status",
            "not-required",
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
            "--evidence-id",
            "E1",
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
            "claim",
            str(vault),
            trace_id,
            "--claim-id",
            "C1",
            "--text",
            "The checked section supports K=60.",
            "--evidence-id",
            "E1",
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
    assert state["schema_version"] == "1.5"
    assert state["events"][1]["evidence_ids"] == ["E1"]
    assert "accepted_count" not in state["events"][1]
    assert state["claims"][0]["evidence_ids"] == ["E1"]
    assert "Accepted evidence: `1`" in note
    assert "## Claim–Evidence map" in note
    assert state["events"][0]["route"] == "hierarchical-search"
    assert state["events"][0]["candidates"][0]["section_id"] == "spray"
    assert "hierarchical_search_used: true" in note
    assert "Runtime trace, not evidence" in note
    assert "session-123" in note
    assert "```dataview" in dashboard


def test_same_session_creates_unique_single_query_traces(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    results = []
    for question in ("First question", "Second question"):
        completed = subprocess.run(
            [
                sys.executable,
                str(TRACE),
                "start",
                str(vault),
                question,
                "--session-id",
                "shared-session",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(json.loads(completed.stdout))

    assert results[0]["trace_id"] != results[1]["trace_id"]
    assert results[0]["trace_id"] != "shared-session"
    assert Path(results[0]["state_path"]).is_file()
    assert Path(results[1]["state_path"]).is_file()


def test_trace_stage_timer_uses_monotonic_duration(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    started = subprocess.run(
        [sys.executable, str(TRACE), "start", str(vault), "Timed query"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(started.stdout)["trace_id"]
    begun = subprocess.run(
        [
            sys.executable,
            str(TRACE),
            "stage-begin",
            str(vault),
            trace_id,
            "--stage",
            "document-reading",
            "--route",
            "converted-source",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stage_id = json.loads(begun.stdout)["stage"]["stage_id"]
    subprocess.run(
        [
            sys.executable,
            str(TRACE),
            "stage-end",
            str(vault),
            trace_id,
            stage_id,
            "--summary",
            "Read one source section.",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    state_path = vault / "_system" / "reports" / "query-traces" / "_data" / f"{trace_id}.query-trace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["open_stages"] == {}
    assert state["events"][0]["stage"] == "document-reading"
    assert state["events"][0]["duration_ms"] >= 0


def test_trace_rejects_event_links_to_unknown_evidence(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    started = subprocess.run(
        [sys.executable, str(TRACE), "start", str(vault), "Evidence consistency"],
        capture_output=True,
        text=True,
        check=True,
    )
    trace_id = json.loads(started.stdout)["trace_id"]
    event = subprocess.run(
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
            "--evidence-id",
            "missing-evidence",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert event.returncode != 0
    assert "event references unknown evidence" in event.stderr


def test_multiple_question_traces_share_request_folder_but_not_trace_id(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    request_id = "request-20260728"
    results = []
    for index, question in enumerate(("First question", "Second question"), start=1):
        started = subprocess.run(
            [
                sys.executable,
                str(TRACE),
                "start",
                str(vault),
                question,
                "--session-id",
                "shared-session",
                "--request-id",
                request_id,
                "--question-index",
                str(index),
                "--query-type",
                "evidence",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(started.stdout)
        subprocess.run(
            [
                sys.executable,
                str(TRACE),
                "finish",
                str(vault),
                result["trace_id"],
                "--status",
                "completed",
                "--evidence-level",
                "gap",
                "--conclusion",
                f"Completed question {index}.",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(result)

    trace_root = vault / "_system" / "reports" / "query-traces"
    request_root = trace_root / request_id
    assert results[0]["trace_id"] != results[1]["trace_id"]
    assert Path(results[0]["note_path"]).parent == request_root
    assert Path(results[1]["note_path"]).parent == request_root
    assert Path(results[0]["request_summary"]) == request_root / "Request Summary.md"
    assert (trace_root / "_data" / f"{results[0]['trace_id']}.query-trace.json").is_file()
    assert (trace_root / "_data" / f"{results[1]['trace_id']}.query-trace.json").is_file()

    summary = (request_root / "Request Summary.md").read_text(encoding="utf-8")
    assert "type: query-trace-request" in summary
    assert "| 1 | `completed` |" in summary
    assert "| 2 | `completed` |" in summary
    assert results[0]["trace_id"] in summary
    assert results[1]["trace_id"] in summary


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


def test_read_only_wording_does_not_disable_default_query_trace() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    reference = TRACE_REFERENCE.read_text(encoding="utf-8")
    assert "Read-only query” protects governed artifacts; it does **not** disable the trace" in skill
    assert "explicit no-trace request or an unwritable Vault" in skill
    assert "verifies the Markdown note exists" in skill
    assert "read-only controlled query still creates its trace" in reference
    assert "trace: unavailable" in reference


def test_query_contract_fuses_parallel_scope_before_governed_first_search() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    workflow = QUERY_WORKFLOW.read_text(encoding="utf-8")
    assert "query_session.py" in skill
    assert "begin -> inspect -> finalize" in skill
    assert "begin -> inspect -> verify -> one visual check when ready -> finalize" in skill
    assert "Consume the fused union" in skill
    assert "Inspect retained `30_Cards/`, `40_Concepts/`, and `50_Projects/` material first" in skill
    assert "Use supplemental scoped exact/lexical search only" in skill
    assert "disabled or unavailable Provider" in workflow
    assert "Supported claims require at least one recorded evidence ID" in workflow
    assert "A table, formula, engineering parameter, image reference, or Bundle QA flag does not trigger it by itself" in skill
    assert "unknown event fields" in skill and "extensions" in skill
    assert "outside the fused top-k" in workflow


def test_skill_name_alone_activates_complete_query_contract() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    description = next(line for line in skill.splitlines() if line.startswith("description: "))
    assert "必须选择 hermes-obsidian-controlled-query" in description
    assert "required non-authoritative query trace" in skill
    assert "Do not create or update governed artifacts during query" in skill


def test_multiple_questions_are_sequential_and_trace_isolated() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    reference = TRACE_REFERENCE.read_text(encoding="utf-8")
    assert "strictly one at a time" in skill
    assert "before starting the next question" in skill
    assert "keep two traces open" in skill
    assert "shared `--request-id`, one-based `--question-index`, and total `--question-count`" in skill
    assert "Do not use a Hermes session ID as a trace ID" in skill
    assert "create an ad hoc orchestration script" in skill
    assert "Each independently answerable question receives its own trace" in reference
    assert "Grouped notes live under `_system/reports/query-traces/<request-id>/`" in reference
    assert "Never reuse a trace ID" in reference
    assert "Map every numbered final answer to its trace path" in reference


def test_user_facing_evidence_uses_original_pdf_and_logs_conversion_carriers() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    answer_format = ANSWER_FORMAT.read_text(encoding="utf-8")
    evidence_levels = EVIDENCE_LEVELS.read_text(encoding="utf-8")
    workflow = QUERY_WORKFLOW.read_text(encoding="utf-8")
    trace_reference = TRACE_REFERENCE.read_text(encoding="utf-8")

    assert "Never substitute a Bundle, Markdown, source-map, ledger" in skill
    assert "Record source maps, ledgers, `document.md`" in skill
    assert "They are internal retrieval and QA details, not user-facing evidence sources" in skill
    assert "Original PDF path: <original PDF path or unresolved>" in answer_format
    assert "Figure/image/table location:" in answer_format
    assert "section + page region, with reliable coordinates when available" in answer_format
    assert "Vault/source path:" not in answer_format
    assert "Source text: <document.md/table/image path" not in answer_format
    assert "Record those verification carriers in the query trace" in answer_format
    assert "record their paths in the query trace" in evidence_levels
    assert "original PDF and page" in workflow
    assert "Report them as converted-source lines" not in workflow
    assert "internal verification carriers, including source maps, ledgers, `document.md`" in trace_reference
    assert "Verification-carrier paths belong in this trace" in trace_reference


def test_runtime_scripts_are_resolved_from_the_active_skill() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    reference = TRACE_REFERENCE.read_text(encoding="utf-8")
    assert "Resolve `<query-skill-root>` from the active loader" in skill
    assert "${HERMES_SKILL_DIR}" in skill
    assert "skill_dir` returned by `skill_view`" in skill
    assert "Do not hard-code an installation directory" in skill
    assert "Never resolve scripts relative to the Vault" in skill
    assert "Do not announce that scripts are missing" in skill
    assert "linked_files.scripts" in skill
    assert "<vault>/_system/skills" in skill
    assert "<query-skill-root>/scripts/query_session.py" in reference
    assert "/root/.hermes/skills" not in skill
    assert "hard-code `/root/.hermes/skills`" in reference


def test_hermes_descriptions_frontload_skill_loading_without_query_script_source() -> None:
    expected_prefixes = {
        ROOT / "hermes-obsidian-controlled-query" / "SKILL.md": "受控查询 / Controlled Query",
        ROOT / "hermes-obsidian-controlled-ingest" / "SKILL.md": "受控摄取 / Controlled Ingest",
        ROOT / "hermes-obsidian-vault-bootstrap" / "SKILL.md": "Vault 初始化 / Vault Bootstrap",
        ROOT / "hermes-obsidian-vault-lint" / "SKILL.md": "Vault 只读审计 / Read-only Vault Lint",
    }
    for path, prefix in expected_prefixes.items():
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        description = next(
            line.removeprefix("description: ").strip()
            for line in frontmatter.splitlines()
            if line.startswith("description: ")
        )
        assert description.startswith(prefix)
        assert "MUST call skill_view" in description
        if path == QUERY_SKILL:
            assert "verify linked_files.scripts includes query_session.py" in description
            assert "without loading their source" in description
            assert "load this full skill first" in description
        else:
            assert "load the full skill first" in description


def test_query_domain_terms_are_configuration_not_frontmatter() -> None:
    skill = QUERY_SKILL.read_text(encoding="utf-8")
    config = json.loads(
        (ROOT / "hermes-obsidian-controlled-query" / "config" / "domain-routing.json").read_text(
            encoding="utf-8"
        )
    )
    description = next(line for line in skill.splitlines() if line.startswith("description: "))
    assert config["domain_query_terms"] == ["消防系统"]
    assert "消防系统" not in description
    assert "config/domain-routing.json" in skill
    assert "domain_query_terms" in skill


def test_query_description_maps_short_query_phrases_to_the_canonical_skill() -> None:
    description = next(
        line.removeprefix("description: ")
        for line in QUERY_SKILL.read_text(encoding="utf-8").splitlines()
        if line.startswith("description: ")
    )
    assert "请使用 query 回答" in description[:160]
    assert "请使用query回答" in description[:160]
    assert "使用 query 查询" in description[:160]
    assert "用 query 查 Vault" in description[:160]
    assert "必须选择 hermes-obsidian-controlled-query" in description[:200]


def test_short_hermes_bundle_aliases_are_deployable() -> None:
    expected = {
        "v-query": "hermes-obsidian-controlled-query",
        "v-ingest": "hermes-obsidian-controlled-ingest",
        "v-bootstrap": "hermes-obsidian-vault-bootstrap",
        "v-lint": "hermes-obsidian-vault-lint",
    }
    for alias, skill_name in expected.items():
        content = (ROOT / "hermes-skill-bundles" / f"{alias}.yaml").read_text(encoding="utf-8")
        assert f"name: {alias}" in content
        assert f"  - {skill_name}" in content


def test_evidence_modes_and_relationship_pass_are_governed() -> None:
    query_skill = QUERY_SKILL.read_text(encoding="utf-8")
    ingest_skill = INGEST_SKILL.read_text(encoding="utf-8")
    assert "`evidence_mode: index`" in query_skill
    assert "`evidence_mode: relational`" in query_skill
    assert "## Evidence Modes" in ingest_skill
    assert "## Knowledge-Graph Relation Pass" in ingest_skill
    assert "do not finish with zero links" in ingest_skill
    assert "Never leave template or generator expressions" in ingest_skill
