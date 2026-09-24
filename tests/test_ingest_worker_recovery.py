"""Real CLI contracts and cancellation/recovery boundaries for source workers."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from test_document_governance import create_engineering_vault, MANAGER
from test_p3_knowledge_build import (
    ROOT, DISPATCH_CLI, ORCHESTRATOR_CLI, FakeKanban, IngestKanbanAdapter,
    ContractError, start_and_pin, worker_pack, workflow_request,
)
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import workflow_write_guard, require_source, worker_binding
from hermes_source_units import FileSourceUnitService


def setup_worker(tmp_path):
    vault = create_engineering_vault(tmp_path)
    paths = ["10_Raw/first.pdf", "10_Raw/second.pdf"]
    for path in paths:
        (vault / path).write_bytes(path.encode())
    adapter = IngestKanbanAdapter(vault, FakeKanban(True), enable_workers=True)
    value = start_and_pin(vault, workflow_request(
        workflow_id="ingest-recovery", actor="agent", expected_revision=0,
        profile="compact-3", scope={"source_paths": paths,
                                    "knowledge_selector": "all-current",
                                    "execution_mode": "canary_only"}))
    adapter.sync(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                                 expected_revision=value["revision"]))
    value = adapter.workflow.status(value["workflow_id"])
    card = value["kanban"]["task_map"][0]
    request = {"workflow_id": value["workflow_id"], "node": card["node"],
               "task_id": card["task_id"]}
    return vault, adapter, value, request


def cli(vault, script, command, request, env=None):
    path = vault / "worker-request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "--vault", str(vault),
                             command, "--request", str(path)],
                            capture_output=True, text=True, encoding="utf-8", env=env)
    return result, json.loads(result.stdout if result.returncode == 0 else result.stderr)


@pytest.mark.parametrize("script", [DISPATCH_CLI, ORCHESTRATOR_CLI])
def test_real_worker_cli_registers_and_reuses_identity_then_stops(tmp_path, script):
    vault, adapter, value, request = setup_worker(tmp_path)
    env = {key: value for key, value in os.environ.items()
           if key not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD")}
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    result, begun = cli(vault, script, "worker-begin", request, env)
    assert result.returncode == 0, begun
    assert begun["assigned_source"]["path"] == "10_Raw/first.pdf"
    assert begun["source_coverage"]["requested"] == 2
    request["template_hash"] = begun["template_hash"]
    result, registered = cli(vault, script, "worker-register-source", request, env)
    assert result.returncode == 0, registered
    assert not registered["reused"]
    result, reused = cli(vault, script, "worker-register-source", request, env)
    assert result.returncode == 0, reused
    assert reused["reused"] and reused["resource_id"] == registered["resource_id"]
    result, heartbeat = cli(vault, script, "worker-heartbeat", request, env)
    assert result.returncode == 0, heartbeat
    result, failed = cli(vault, script, "worker-fail", {
        **request, "code": "SCOPE_MISMATCH", "message": "test failure report"}, env)
    assert result.returncode == 0 and failed["kanban_reconciliation_pending"]
    result, denied = cli(vault, script, "sync", request, env)
    assert result.returncode == 2 and denied["code"] == "ACCESS_DENIED"
    bound = subprocess.run([sys.executable, str(MANAGER), "--vault", str(vault),
        "--worker-binding", str(vault / "worker-request.json"),
        "organization-add", "--organization-id", "organization-bound", "--name", "bound",
        "--expected-revision", "1", "--actor", "agent"], env=env,
        capture_output=True, text=True, encoding="utf-8")
    assert bound.returncode == 0, bound.stderr
    registry = vault / "_system/metadata/document-registry.json"
    before = registry.read_bytes()
    record = json.loads(before)["records"][0]
    assert record["governance_status"] == "candidate"
    assert record["authority_status"] == "unknown" and record["business_version"] is None
    current = adapter.workflow.status(value["workflow_id"])
    adapter.workflow.cancel(workflow_request(workflow_id=value["workflow_id"],
                           actor="agent", expected_revision=current["revision"]))
    result, error = cli(vault, script, "worker-register-source", request, env)
    assert result.returncode == 2 and error["code"] == "WORKFLOW_STOPPED"
    # Also exercise the previously unguarded domain CLI, without dispatch.
    result = subprocess.run([sys.executable, str(MANAGER), "--vault", str(vault),
        "--worker-binding", str(vault / "worker-request.json"),
        "organization-add", "--organization-id", "organization-late", "--name", "late",
        "--expected-revision", "2", "--actor", "agent"], env=env,
        capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2 and "WORKFLOW_STOPPED" in result.stderr
    unbound = subprocess.run([sys.executable, str(MANAGER), "--vault", str(vault),
        "organization-add", "--organization-id", "organization-late", "--name", "late",
        "--expected-revision", "2", "--actor", "agent"], env=env,
        capture_output=True, text=True, encoding="utf-8")
    assert unbound.returncode == 2 and "ACCESS_DENIED" in unbound.stderr
    assert registry.read_bytes() == before
    organizations = json.loads((vault / "_system/metadata/source-organizations.json").read_text())
    assert len(organizations["organizations"]) == 2


def test_worker_guard_serializes_cancel_and_rejects_other_source(tmp_path, monkeypatch):
    vault, adapter, value, request = setup_worker(tmp_path)
    with worker_binding(request):
        prepared = FileSourceUnitService(vault).prepare_markdown(
            "10_Raw/first.pdf", "doc-test", "version-test", "resource-test")
    registry_path = vault / "_system/metadata/document-registry.json"
    registry = json.loads(registry_path.read_text())
    registry["records"] = [{"document_id": "doc-test", "version_id": "version-test",
        "resource_id": "resource-test", "processing_status": "completed",
        "content_sha256": hashlib.sha256((vault / "10_Raw/first.pdf").read_bytes()).hexdigest()}]
    registry_path.write_text(json.dumps(registry))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    cancel = workflow_request(workflow_id=value["workflow_id"], actor="agent",
                              expected_revision=value["revision"])
    with worker_binding(request), workflow_write_guard(vault, actor="agent") as context:
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            require_source(context, hashlib.sha256(b"other").hexdigest())
        with pytest.raises(ContractError, match="REVISION_CONFLICT"):
            adapter.workflow.cancel(cancel)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        with worker_binding({**request, "node": "exact-plan"}), workflow_write_guard(vault):
            pass
    adapter.workflow.cancel(cancel)
    with pytest.raises(ContractError, match="WORKFLOW_STOPPED"):
        with worker_binding(request), workflow_write_guard(vault):
            pytest.fail("cancelled worker entered mutation")
    service = FileSourceUnitService(vault)
    with pytest.raises(ContractError, match="WORKFLOW_STOPPED"):
        with worker_binding(request):
            service.prepare_markdown("10_Raw/first.pdf", "doc-test", "version-test", "resource-test")
    with pytest.raises(ContractError, match="WORKFLOW_STOPPED"):
        with worker_binding(request):
            service.build({"artifact_manifest": prepared["artifact_manifest"],
                           "config": service.config, "actor": "agent", "expected_revision": 0})
    assert service._current("resource-test") is None


def test_scope_error_is_not_source_failure_and_repair_preserves_audit(tmp_path):
    vault, adapter, value, request = setup_worker(tmp_path)
    begun = adapter.worker_begin(request)
    request["template_hash"] = begun["template_hash"]
    result = adapter.worker_fail({**request, "code": "SCOPE_MISMATCH",
                                  "message": "mistaken whole-workflow scope"})
    assert not result["ok"]
    current = adapter.workflow.status(value["workflow_id"])
    assert current["source_outcomes"] == []
    with pytest.raises(ContractError, match="INVALID_SOURCE_FAILURE"):
        adapter.workflow.record_source_outcome(workflow_request(
            workflow_id=value["workflow_id"], actor="agent", expected_revision=current["revision"],
            **begun["source"], status="failed", error_code="SCOPE_MISMATCH", reason="wrong"))
    # Simulate an old release's erroneous immutable outcome, then cancel normally.
    mistaken = {**begun["source"], "status": "failed", "resource_id": None,
                "unit_set_id": None, "error_code": "SCOPE_MISMATCH",
                "reason": "legacy wrong scope", "artifact_refs": []}
    current["source_outcomes"] = [mistaken]
    adapter.workflow._write(current)
    stopped = adapter.workflow.cancel(workflow_request(
        workflow_id=value["workflow_id"], actor="agent", expected_revision=current["revision"]))
    evidence = vault / "_system/reports/repair-evidence.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("Confirmed worker scope interpretation error.")
    pack = [{**item, "content": item["content"] + "\nRepaired contract.\n"} for item in worker_pack()]
    repair = workflow_request(workflow_id=value["workflow_id"], actor="agent",
        expected_revision=stopped["revision"], repair_id="repair-1", reason="proven scope error",
        evidence_refs=[evidence.relative_to(vault).as_posix()], templates=pack,
        execution_mode="canary_only", reset_sources=[{
            "path": mistaken["path"], "outcome_digest": "sha256:" + fingerprint(mistaken)}])
    repaired = adapter.workflow.repair_preparation(repair)
    assert repaired["state"] == "cancelled" and repaired["cancel_requested"]
    assert repaired["source_outcomes"] == [] and repaired["kanban"]["task_map"] == []
    audit = repaired["repair_history"][0]
    before = (vault / audit["before_ref"]).read_bytes()
    assert hashlib.sha256(before).hexdigest() == audit["before_sha256"]
    assert json.loads(before)["source_outcomes"] == [mistaken]
    assert adapter.workflow.repair_preparation(repair) == repaired
    resumed = adapter.workflow.resume(workflow_request(workflow_id=value["workflow_id"],
        actor="agent", expected_revision=repaired["revision"]))
    adapter.sync(workflow_request(workflow_id=value["workflow_id"],
        actor="agent", expected_revision=resumed["revision"]))
    with pytest.raises(ContractError, match="STALE_INPUT"):
        adapter.worker_check(request)
    assert adapter.workflow.status(value["workflow_id"])["workflow_id"] == value["workflow_id"]
