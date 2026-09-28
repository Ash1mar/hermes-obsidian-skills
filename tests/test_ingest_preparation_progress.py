"""Idempotent bindings, attempt telemetry and bounded source preparation."""
import json
from pathlib import Path
import subprocess
import os

import pytest

from test_ingest_worker_recovery import setup_worker, cli
from test_document_governance import create_bundle
from test_p3_knowledge_build import workflow_request, ORCHESTRATOR_CLI, desired_graph
from ingest_kanban import runtime_projection, runtime_log_observations
from source_preparation import prepare_source
from hermes_source_units import ContractError


def test_identical_binding_is_read_only_but_stale_revision_is_rejected(tmp_path, monkeypatch):
    vault, adapter, value, _ = setup_worker(tmp_path)
    path = adapter.workflow._path(value["workflow_id"])
    before = path.read_bytes(), path.stat().st_mtime_ns
    request = workflow_request(workflow_id=value["workflow_id"], actor=value["actor"],
        expected_revision=value["revision"], board_id=value["kanban"]["board_id"],
        task_map=list(reversed(value["kanban"]["task_map"])))
    monkeypatch.setattr(adapter.workflow, "_write", lambda *_: pytest.fail("identical bind wrote ledger"))
    assert adapter.workflow.bind_kanban(request)["revision"] == value["revision"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    request["expected_revision"] -= 1
    from hermes_source_units import mutation_digest
    request["input_digest"] = mutation_digest(request)
    with pytest.raises(ContractError, match="REVISION_CONFLICT"):
        adapter.workflow.bind_kanban(request)


def test_changed_binding_increments_once_and_repeated_sync_stays_stable(tmp_path):
    _, adapter, value, _ = setup_worker(tmp_path)
    for _ in range(3):
        adapter._sync_current(value["workflow_id"])
    assert adapter.workflow.status(value["workflow_id"])["revision"] == value["revision"]
    mapping = [dict(item) for item in value["kanban"]["task_map"]]
    mapping[0]["task_id"] = "t-replaced"
    result = adapter.workflow.bind_kanban(workflow_request(
        workflow_id=value["workflow_id"], actor=value["actor"],
        expected_revision=value["revision"], board_id=value["kanban"]["board_id"], task_map=mapping))
    assert result["revision"] == value["revision"] + 1


@pytest.mark.parametrize("error,category", [
    ("APIConnectionError <- ConnectError", "model_connection_failed"),
    ("RateLimitError HTTP 429", "model_rate_limited"),
    ("insufficient_quota", "quota_exhausted"),
    ("", "provider_retry_reason_unknown"),
    ("pid 42 exited rate-limited (quota wall) — requeued without counting a failure", "provider_retry_reason_unknown"),
])
def test_requeued_provider_attempt_does_not_invent_quota_or_deadline(error, category):
    result = runtime_projection({"task": {"id": "t", "status": "ready"}, "runs": [
        {"id": 1, "started_at": 10, "ended_at": 20, "outcome": "rate_limited", "error": error}]})
    assert result["state"] == "retry_wait"
    assert result["error_category"] == category
    assert result["retry_not_before"] is None


def test_active_new_attempt_does_not_inherit_previous_error():
    result = runtime_projection({"task": {"id": "t", "status": "running", "last_failure_error": "quota exhausted"},
        "runs": [{"id": 9, "ended_at": 20, "outcome": "rate_limited", "error": "ConnectError"},
                 {"id": 10, "ended_at": None, "outcome": None, "error": None}]})
    assert result["state"] == "running" and result["error_category"] is None
    assert result["last_failed_attempt"]["error_category"] == "model_connection_failed"


def test_recent_log_observations_are_explicitly_not_current_attempt_evidence():
    result = runtime_log_observations("API request failed: error_type=APIConnectionError exception chain: ConnectError\n"
                                      "API request failed: error_type=RateLimitError HTTP 429\n")
    assert result["error_categories"] == ["model_connection_failed", "model_rate_limited"]
    assert result["current_attempt_proven"] is False


def test_runtime_status_is_read_only_and_partial_unavailability_is_explicit(tmp_path, monkeypatch):
    _, adapter, value, _ = setup_worker(tmp_path)
    path = adapter.workflow._path(value["workflow_id"])
    before = path.read_bytes()
    first = value["kanban"]["task_map"][0]["task_id"]
    def snapshot(board, task):
        if task != first:
            raise OSError("native unavailable")
        return {"task": {"id": task, "status": "ready"}, "runs": [
            {"id": 1, "ended_at": 20, "outcome": "rate_limited", "error": "ConnectError"}]}
    monkeypatch.setattr(adapter.kanban, "task_snapshot", snapshot, raising=False)
    result = adapter.runtime_status(value["workflow_id"])
    assert result["cards"][0]["state"] == "retry_wait"
    assert result["cards"][1]["state"] == "unknown"
    assert path.read_bytes() == before


def test_isolated_cli_prepares_one_explicit_bundle_and_keeps_canary_gated(tmp_path):
    vault, adapter, value, request = setup_worker(tmp_path)
    begun = adapter.worker_begin(request)
    bundle = create_bundle(vault, vault / begun["assigned_source"]["path"])
    result, payload = cli(vault, ORCHESTRATOR_CLI, "worker-prepare-source",
        {**request, "bundle": bundle.relative_to(vault).as_posix()},
        env={**os.environ, "HERMES_DELEGATED_CHILD_CONTEXT": "1"})
    assert result.returncode == 0, result.stderr
    assert payload["ok"] and payload["kanban_reconciliation_pending"]
    current = adapter.workflow.status(value["workflow_id"])
    assert len(current["source_outcomes"]) == 1 and current["batch_id"] is None
    assert current["current_stage"] == "source_preparing"
    assert current["dispatch_policy"]["mode"] == "disabled"
    graph = desired_graph(adapter.workflow, current)
    assert not adapter._eligible(current, next(n for n in graph if n.name == "exact-plan"), graph)
    registry = json.loads((vault / "_system/metadata/document-registry.json").read_text())
    record = registry["records"][0]
    assert record["processing_status"] == "completed"
    assert record["governance_status"] == "candidate" and record["authority_status"] == "unknown"


@pytest.mark.parametrize("failure", ["wrong_hash", "cancelled", "invalid_bundle"])
def test_preparation_rejects_identity_cancel_and_bad_quality_without_source_failure(tmp_path, failure):
    vault, adapter, value, request = setup_worker(tmp_path)
    begun = adapter.worker_begin(request)
    bundle = create_bundle(vault, vault / begun["assigned_source"]["path"])
    if failure == "wrong_hash":
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["source"]["sha256"] = "0" * 64
        (bundle / "manifest.json").write_text(json.dumps(manifest))
    elif failure == "invalid_bundle":
        (bundle / "document.md").write_text("")
    else:
        adapter.workflow.cancel(workflow_request(workflow_id=value["workflow_id"],
            actor=value["actor"], expected_revision=value["revision"]))
    with pytest.raises(ContractError):
        prepare_source(adapter, {**request, "bundle": bundle.relative_to(vault).as_posix()})
    assert adapter.workflow.status(value["workflow_id"])["source_outcomes"] == []


def test_conversion_retries_once_keeps_evidence_and_uses_explicit_binding(tmp_path, monkeypatch):
    vault, adapter, value, request = setup_worker(tmp_path)
    calls = []
    def convert(argv, **kwargs):
        calls.append(argv)
        source = Path(argv[2])
        destination = Path(argv[argv.index("-o") + 1])
        assert "--worker-binding" in argv and "--vault" in argv
        temporary = create_bundle(vault, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(destination)
        if len(calls) == 1:
            (destination / "document.md").write_text("")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr("source_preparation.subprocess.run", convert)
    result = prepare_source(adapter, request)
    assert result["ok"] and len(calls) == 2
    assert calls[1][calls[1].index("--backend") + 1] == "pipeline"
    reports = list(vault.glob("_system/ledgers/ingest-workflows/*/source-preparation/*/validation-*.json"))
    assert len(reports) == 2
    assert {json.loads(p.read_text())["status"] for p in reports} == {"fail", "pass"}


def test_runtime_conversion_error_does_not_retry_or_close_source(tmp_path, monkeypatch):
    _, adapter, value, request = setup_worker(tmp_path)
    calls = []
    def failure(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 2)
    monkeypatch.setattr("source_preparation.subprocess.run", failure)
    with pytest.raises(ContractError, match="PREPARATION_RUNTIME_FAILED"):
        prepare_source(adapter, request)
    assert len(calls) == 1
    assert adapter.workflow.status(value["workflow_id"])["source_outcomes"] == []
    # A wrong-kind request must not begin or lease another kind of worker.
    monkeypatch.setattr(adapter, "worker_begin", lambda *_: pytest.fail("wrong-kind begin"))
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        prepare_source(adapter, {**request, "node": "pass-slice:other"})


def test_resume_after_publication_reuses_current_without_conversion(tmp_path, monkeypatch):
    vault, adapter, value, request = setup_worker(tmp_path)
    begun = adapter.worker_begin(request)
    bundle = create_bundle(vault, vault / begun["assigned_source"]["path"])
    complete = adapter.worker_complete
    def interrupted(_):
        raise ContractError("INTERRUPTED", "$", "simulated exit after publication")
    monkeypatch.setattr(adapter, "worker_complete", interrupted)
    with pytest.raises(ContractError, match="INTERRUPTED"):
        prepare_source(adapter, {**request, "bundle": bundle.relative_to(vault).as_posix()})
    assert adapter.workflow.status(value["workflow_id"])["source_outcomes"] == []
    monkeypatch.setattr(adapter, "worker_complete", complete)
    monkeypatch.setattr("source_preparation.subprocess.run", lambda *_a, **_kw: pytest.fail("resume converted again"))
    result = prepare_source(adapter, request)
    assert result["ok"] and result["reused_current"]
    assert len(adapter.workflow.status(value["workflow_id"])["source_outcomes"]) == 1


def test_cancellation_during_conversion_prevents_governance_and_unit_commits(tmp_path, monkeypatch):
    vault, adapter, value, request = setup_worker(tmp_path)
    def cancelled(argv, **kwargs):
        destination = Path(argv[argv.index("-o") + 1])
        temporary = create_bundle(vault, Path(argv[2]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(destination)
        latest = adapter.workflow.status(value["workflow_id"])
        adapter.workflow.cancel(workflow_request(workflow_id=value["workflow_id"],
            actor=value["actor"], expected_revision=latest["revision"]))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr("source_preparation.subprocess.run", cancelled)
    with pytest.raises(ContractError, match="WORKFLOW_STOPPED"):
        prepare_source(adapter, request)
    assert not list(vault.glob("_system/sources/units/*/current.json"))
    registry = json.loads((vault / "_system/metadata/document-registry.json").read_text())
    assert registry["records"][0]["processing_status"] == "processing"
    assert adapter.workflow.status(value["workflow_id"])["source_outcomes"] == []
