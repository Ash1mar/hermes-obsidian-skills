"""Operator continuation gates cannot be bypassed by recovery or stale workers."""
import json

import pytest

from test_p3_knowledge_build import (vault, plan_sliced_batch, FakeKanban,
    IngestKanbanAdapter, start_and_pin, workflow_request, publish_sources, ROOT)
from hermes_source_units import ContractError
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


def paused_plan(vault):
    plan_sliced_batch(vault, 27, "pause-batch")
    adapter = IngestKanbanAdapter(vault, FakeKanban(True), enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id="ingest-pauses", actor="agent",
        expected_revision=0, profile="compact-3", batch_id="pause-batch",
        scope={"source_paths": [], "knowledge_selector": "all-current", "execution_mode": "auto_full",
               "pause_after": ["exact_plan", "canary", "pass"]}))
    result = adapter.sync(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                                          expected_revision=value["revision"]))
    assert result["state"] == "paused" and not result["background_dispatch"]
    return adapter, adapter.workflow.status(value["workflow_id"])


def continuation(value, **changes):
    control = value["pause_control"]
    return workflow_request(workflow_id=value["workflow_id"], actor=value["actor"],
        expected_revision=value["revision"], continue_id="continue-" + control["boundary"],
        boundary=control["boundary"], evidence_digest=control["evidence_digest"], **changes)


def test_plan_pause_survives_resume_restart_and_blocks_all_writes(vault):
    adapter, value = paused_plan(vault)
    assert value["dispatch_policy"]["mode"] == "disabled"
    assert not adapter.kanban.unblocked
    before = adapter.workflow._path(value["workflow_id"]).read_bytes()
    # Recovery is not continuation, including a fresh adapter after a restart.
    restarted = IngestKanbanAdapter(vault, adapter.kanban, enable_workers=True)
    restarted.resume(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                                      expected_revision=value["revision"]))
    assert restarted.workflow._path(value["workflow_id"]).read_bytes() == before
    assert not value["kanban"]["task_map"]  # No unopened Pass/Reduce cards are projected.
    sid = adapter.workflow.knowledge._slices(value["batch_id"])[0]["slice_id"]
    binding = {"workflow_id": value["workflow_id"], "task_id": "late-stale-worker", "node": "pass-slice:" + sid}
    with pytest.raises(ContractError, match="WORKFLOW_PAUSED"):
        adapter.worker_begin(binding)
    with pytest.raises(ContractError, match="WORKFLOW_PAUSED"):
        with worker_binding(binding), workflow_write_guard(vault, actor="agent"):
            pytest.fail("paused worker entered domain write")
    with pytest.raises(ContractError, match="WORKFLOW_PAUSED"):
        adapter.workflow.reconcile(workflow_request(workflow_id=value["workflow_id"], actor="agent",
            expected_revision=value["revision"], target_stage="reducing"))
    req = continuation(value)
    for field, wrong, code in (("actor", "another-actor", "ACTOR_MISMATCH"),
                              ("expected_revision", value["revision"] - 1, "REVISION_CONFLICT"),
                              ("boundary", "canary", "INVALID_TRANSITION")):
        from hermes_source_units import mutation_digest
        rejected = {**req, field: wrong}
        rejected["input_digest"] = mutation_digest(rejected)
        with pytest.raises(ContractError, match=code):
            adapter.continue_workflow(rejected)
        assert adapter.workflow._path(value["workflow_id"]).read_bytes() == before
    result = adapter.continue_workflow(req)
    assert result["background_dispatch"] and result["pause"]["boundary"] is None
    current = adapter.workflow.status(value["workflow_id"])
    assert current["dispatch_policy"]["mode"] == "canary"
    # The exact same request is idempotent despite its old expected_revision.
    adapter.continue_workflow(req)
    assert adapter.workflow.status(value["workflow_id"])["revision"] == current["revision"]


@pytest.mark.parametrize("change", ["metadata", "task", "pause_report", "digest"])
def test_continuation_rechecks_authoritative_evidence(vault, change):
    adapter, value = paused_plan(vault)
    req = continuation(value)
    if change == "metadata":
        path = vault / "_system/metadata/source-organizations.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "task":
        batch = adapter.workflow.knowledge._batch(value["batch_id"])
        path = adapter.workflow.knowledge._task_path(batch["task_ids"][0])
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "pause_report":
        path = vault / value["pause_control"]["report_ref"]
        report = json.loads(path.read_text())
        report["evidence"]["stage"] = "reducing"
        path.write_text(json.dumps(report))
    else:
        req["evidence_digest"] = "sha256:" + "0" * 64
        from hermes_source_units import mutation_digest
        req["input_digest"] = mutation_digest(req)
    before = adapter.workflow._path(value["workflow_id"]).read_bytes()
    with pytest.raises(ContractError, match="STALE_INPUT"):
        adapter.continue_workflow(req)
    assert adapter.workflow._path(value["workflow_id"]).read_bytes() == before


def test_worker_cannot_continue_and_cancel_resume_preserves_pause(vault, monkeypatch):
    adapter, value = paused_plan(vault)
    with monkeypatch.context() as env:
        env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            adapter.continue_workflow(continuation(value))
    adapter.cancel(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                                    expected_revision=value["revision"]))
    current = adapter.workflow.status(value["workflow_id"])
    adapter.resume(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                                    expected_revision=current["revision"]))
    assert adapter.workflow.pending_pause(adapter.workflow.status(value["workflow_id"])) == "exact_plan"


@pytest.mark.parametrize("pause_after", [["unknown"], ["canary", "canary"]])
def test_invalid_pause_policy_rejected(vault, pause_after):
    with pytest.raises(ContractError, match="INVALID_SCHEMA"):
        start_and_pin(vault, workflow_request(workflow_id="ingest-invalid-pause", actor="agent",
            expected_revision=0, profile="compact-3", scope={"source_paths": [],
            "knowledge_selector": "all-current", "execution_mode": "auto_full", "pause_after": pause_after}))


def test_exact_plan_commits_during_projection_and_watcher_waits_for_receipt(vault, monkeypatch):
    import hashlib
    import importlib.util
    refs = publish_sources(vault, ["# Evidence\nGrounded planning.\n"])
    fake = FakeKanban(True)
    adapter = IngestKanbanAdapter(vault, fake, enable_workers=True)
    value = start_and_pin(vault, workflow_request(workflow_id="ingest-plan-race", actor="agent",
        expected_revision=0, profile="compact-3", scope={"source_paths": ["10_Raw/source-1.md"],
        "knowledge_selector": "all-current", "execution_mode": "auto_full", "pause_after": ["exact_plan"]}))
    ref = refs[0]["unit_ref"]
    value = adapter.workflow.record_source_outcome(workflow_request(workflow_id=value["workflow_id"],
        actor="agent", expected_revision=value["revision"], path="10_Raw/source-1.md", status="ready",
        content_sha256=hashlib.sha256((vault / "10_Raw/source-1.md").read_bytes()).hexdigest(),
        resource_id=ref["resource_id"], unit_set_id=ref["unit_set_id"]))
    adapter.workflow.knowledge.plan_batch({"batch_id": "race-batch", "actor": "agent",
        "registry_revision": 1, "exact_reading_budget": True,
        "tasks": [{"task_id": "race-task", "target_refs": refs}]})
    original = fake.create_node
    committed = []
    def commit_during_projection(*args, **kwargs):
        result = original(*args, **kwargs)
        if not committed:
            current = adapter.workflow.status(value["workflow_id"])
            adapter.workflow.reconcile(workflow_request(workflow_id=value["workflow_id"], actor="agent",
                expected_revision=current["revision"], target_stage="analyzing", batch_id="race-batch"))
            committed.append(True)
        return result
    monkeypatch.setattr(fake, "create_node", commit_during_projection)
    # Exercise the actual watcher: its first sync sees the new boundary but cannot
    # snapshot it until the next sync initializes the adopted batch's slices.
    spec = importlib.util.spec_from_file_location("pause_watch", ROOT /
        "hermes-obsidian-governed-ingest-orchestrator/scripts/watch_ingest_workflow.py")
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    monkeypatch.setattr(watcher, "IngestKanbanAdapter", lambda *a, **k: adapter)
    sleeps = []
    monkeypatch.setattr(watcher.time, "sleep", lambda duration: sleeps.append(duration))
    watcher.watch(str(vault), value["workflow_id"])
    current = adapter.workflow.status(value["workflow_id"])
    assert sleeps and current["pause_control"]["evidence_digest"]
    assert current["pause_control"]["boundary"] == "exact_plan"
    assert adapter.workflow.knowledge._batch("race-batch")["slices_initialized"]
    assert not fake.unblocked
