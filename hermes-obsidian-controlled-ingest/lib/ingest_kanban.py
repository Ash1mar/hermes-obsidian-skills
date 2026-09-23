"""Rebuildable Hermes Kanban projection of a Vault ingest workflow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from hermes_source_units import (ContractError, FileIngestWorkflowService,
                                 mutation_digest)
from hermes_source_units.source_units import _exclusive_lock, _load_json, _vault_path
from hermes_source_units.validation import fingerprint, validate_record


def _fail(code: str, message: str) -> None:
    raise ContractError(code, "$", message)


@dataclass(frozen=True)
class Node:
    name: str
    kind: str
    parents: tuple[str, ...]
    input_fingerprint: str
    gate: bool = False


def desired_graph(service: FileIngestWorkflowService, workflow: Mapping[str, Any]) -> list[Node]:
    """Derive the DAG from Vault records, never from Kanban's task state."""
    nodes: list[Node] = []
    workflow_id = workflow["workflow_id"]
    template_hashes = {item["kind"]: item["template_hash"]
                       for item in workflow.get("template_pins", [])}

    def add(name: str, kind: str, parents: tuple[str, ...], inputs: Any,
            gate: bool = False) -> str:
        identity = {
            "workflow_id": workflow_id, "kind": kind, "inputs": inputs,
            "parent_keys": [next(item.input_fingerprint for item in nodes if item.name == parent)
                            for parent in parents],
        }
        if kind in template_hashes:
            identity["worker_template_hash"] = template_hashes[kind]
        nodes.append(Node(name, kind, parents, "sha256:" + fingerprint(identity), gate))
        return name

    batch_id = workflow["batch_id"]
    if batch_id is None:
        sources = []
        outcomes = {item["path"]: item for item in workflow.get("source_outcomes", [])}
        for path in workflow["scope"]["source_paths"]:
            source = _vault_path(service.vault, path)
            if not source.is_file():
                _fail("SOURCE_UNAVAILABLE", f"source path is not a file: {path}")
            identity = {"path": path,
                        "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
            if path in outcomes and outcomes[path]["content_sha256"] != identity["content_sha256"]:
                _fail("SOURCE_CHANGED", f"source bytes changed after outcome: {path}")
            digest = fingerprint(identity)
            sources.append(add(f"source-prepare:{digest[:16]}", "source-prepare", (), identity))
        add("exact-plan", "exact-plan", tuple(sources),
            {"scope": workflow["scope"], "source_outcomes": workflow.get("source_outcomes", [])})
        return nodes

    batch = service.knowledge._batch(batch_id)
    slices = service.knowledge._slices(batch_id)
    pass_nodes: dict[str, str] = {}
    for item in slices:
        name = f"pass-slice:{item['slice_id']}"
        add(name, "pass-slice", (), {"slice_id": item["slice_id"],
                                    "input_fingerprint": item["input_fingerprint"],
                                    "template_hash": item["template_hash"]})
        for task_id in item["task_ids"]:
            pass_nodes[task_id] = name

    resource_tasks: dict[str, list[str]] = {}
    for task_id in batch["task_ids"]:
        task = service.knowledge._task(task_id)
        for resource_id in sorted({ref["unit_ref"]["resource_id"]
                                   for ref in task["target_refs"]}):
            resource_tasks.setdefault(resource_id, []).append(task_id)
    reducers = []
    for resource_id, task_ids in sorted(resource_tasks.items()):
        parents = tuple(sorted({pass_nodes[task_id] for task_id in task_ids
                                if task_id in pass_nodes}))
        pass_ids = []
        for task_id in task_ids:
            root = _vault_path(service.vault,
                               f"_system/knowledge-builds/task-{task_id}/passes")
            for path in sorted(root.glob("*.json")) if root.exists() else []:
                record = _load_json(path)
                validate_record("knowledge_pass", record)
                pass_ids.append(record["pass_id"])
        reducers.append(add(f"resource-reduce:{resource_id}", "resource-reduce",
                            parents, {"resource_id": resource_id,
                                      "task_ids": sorted(task_ids),
                                      "pass_ids": sorted(pass_ids),
                                      "batch_id": batch_id}))
    global_node = add("global-reduce", "global-reduce", tuple(reducers),
                      {"batch_id": batch_id,
                       "resource_reduction_ids": sorted(batch.get("resource_reduction_ids", []))})
    validator = add("checkpoint-1-validate", "checkpoint-1-validate",
                    (global_node,), batch_id)
    gate1 = add("checkpoint-1-gate", "checkpoint-1-gate", (validator,), batch_id,
                gate=True)
    build = add("build-finalize", "build-finalize", (gate1,), batch_id)
    release = add("vault-finalize-plan", "vault-finalize-plan", (build,), batch_id)
    validator2 = add("checkpoint-2-validate", "checkpoint-2-validate",
                     (release,), batch_id)
    gate2 = add("checkpoint-2-gate", "checkpoint-2-gate", (validator2,), batch_id,
                gate=True)
    applied = add("release-apply", "release-apply", (gate2,), batch_id)
    provider = workflow["scope"].get("provider", "skip")
    if provider == "sync":
        applied = add("provider-sync", "provider-sync", (applied,), batch_id)
    add("acceptance", "acceptance", (applied,), batch_id)
    return nodes


def board_slug(workflow_id: str) -> str:
    return "ingest-" + fingerprint(workflow_id)[:24]


class KanbanCLI:
    def __init__(self, invoke: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None):
        self.invoke = invoke or self._invoke

    @staticmethod
    def _invoke(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", check=False)

    def command(self, *parts: str) -> str:
        result = self.invoke(["hermes", *parts])
        if result.returncode != 0:
            _fail("KANBAN_UNAVAILABLE", result.stderr.strip() or result.stdout.strip()
                  or "Hermes Kanban command failed")
        return result.stdout

    def dispatcher_available(self) -> bool:
        try:
            result = self.invoke(["hermes", "gateway", "status"])
        except OSError:
            return False
        output = result.stdout + result.stderr
        if (result.returncode != 0 or "Gateway is running" not in output
                or "not running" in output.lower()):
            return False
        try:
            location = self.invoke(["hermes", "config", "path"])
            if location.returncode != 0:
                return False
            lines = Path(location.stdout.strip()).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return False
        in_kanban = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line[:1].isspace():
                in_kanban = stripped == "kanban:"
            elif in_kanban and stripped.startswith("dispatch_in_gateway:"):
                return stripped.partition(":")[2].split("#", 1)[0].strip().lower() == "true"
        return True  # Hermes defaults this setting to enabled.

    def ensure_board(self, slug: str) -> None:
        boards = json.loads(self.command("kanban", "boards", "list", "--json"))
        if any(item["slug"] == slug for item in boards):
            return
        self.command("kanban", "boards", "create", slug,
                     "--name", f"Ingest {slug}")

    def create_node(self, slug: str, node: Node, key: str,
                    parent_ids: list[str], body: str,
                    *, enable_workers: bool) -> str:
        argv = ["kanban", "--board", slug, "create", node.name,
                "--body", body, "--idempotency-key", key,
                "--created-by", "ingest-workflow", "--assignee", "default",
                "--max-retries", "1", "--json"]
        for parent in parent_ids:
            argv.extend(("--parent", parent))
        if node.gate or not enable_workers:
            argv.extend(("--initial-status", "blocked"))
        task = json.loads(self.command(*argv))
        if not isinstance(task.get("id"), str) or not task["id"]:
            _fail("KANBAN_INVALID_RESPONSE", "task creation omitted its id")
        return task["id"]

    def complete(self, slug: str, task_id: str, result: str) -> None:
        if self.task_status(slug, task_id) == "done":
            return
        self.command("kanban", "--board", slug, "complete", task_id,
                     "--result", result)

    def task_status(self, slug: str, task_id: str) -> str:
        shown = json.loads(self.command("kanban", "--board", slug, "show",
                                        task_id, "--json"))
        return str(shown["task"]["status"])

    def wait(self, slug: str, task_id: str, state: str, reason: str) -> None:
        if self.task_status(slug, task_id) in (state, "done", "archived"):
            return
        operation = "schedule" if state == "scheduled" else "block"
        self.command("kanban", "--board", slug, operation, task_id, reason)

    def unblock(self, slug: str, task_id: str) -> None:
        if self.task_status(slug, task_id) in ("blocked", "scheduled"):
            self.command("kanban", "--board", slug, "unblock", task_id,
                         "--reason", "Vault slice ready")


def domain_completed(service: FileIngestWorkflowService,
                     workflow: Mapping[str, Any], node: Node) -> bool:
    if node.kind == "source-prepare":
        outcomes = {item["path"]: item for item in workflow.get("source_outcomes", [])}
        return any(node.name == "source-prepare:" + fingerprint({
            "path": path, "content_sha256": item["content_sha256"]})[:16]
                   for path, item in outcomes.items())
    if node.kind == "exact-plan":
        return workflow["batch_id"] is not None
    if node.kind == "acceptance" and workflow["state"] == "partial":
        return True
    if node.kind == "pass-slice":
        slice_id = node.name.partition(":")[2]
        return service.knowledge._slice(workflow["batch_id"], slice_id)["state"] == "completed"
    if node.kind == "resource-reduce":
        resource_id = node.name.partition(":")[2]
        batch = service.knowledge._batch(workflow["batch_id"])
        if (not batch.get("resource_reduction_ids") and batch["run_ids"]
                and batch["state"] in ("checkpoint_1", "finalizing", "completed")):
            return True  # valid legacy direct Reduce adoption
        return any(service.knowledge._resource_reduction(workflow["batch_id"], rid)["resource_id"]
                   == resource_id for rid in batch.get("resource_reduction_ids", []))
    if node.kind == "global-reduce":
        batch = service.knowledge._batch(workflow["batch_id"])
        return (batch.get("global_reduction_id") is not None or
                (bool(batch["run_ids"]) and batch["state"] in (
                    "checkpoint_1", "finalizing", "completed")))
    if node.kind == "checkpoint-1-validate":
        return service.knowledge._batch(workflow["batch_id"])["state"] in (
            "checkpoint_1", "finalizing", "completed")
    if node.kind == "checkpoint-1-gate":
        return workflow["checkpoints"]["checkpoint_1"]["state"] == "approved"
    if node.kind == "checkpoint-2-gate":
        return workflow["checkpoints"]["checkpoint_2"]["state"] == "approved"
    if node.kind == "build-finalize":
        status = service.knowledge.batch_status(workflow["batch_id"], compact=True)
        return bool(status["runs"]) and all(item["state"] == "completed"
                                                for item in status["runs"])
    stages = ("created", "source_preparing", "planning", "analyzing", "reducing",
              "checkpoint_1", "build_finalizing", "release_planning", "checkpoint_2",
              "applying", "indexing", "validating", "completed")
    stage = workflow["current_stage"]
    thresholds = {"source-prepare": "planning", "exact-plan": "analyzing",
                  "vault-finalize-plan": "checkpoint_2",
                  "checkpoint-2-validate": "checkpoint_2",
                  "release-apply": "indexing", "provider-sync": "validating",
                  "acceptance": "completed"}
    threshold = thresholds.get(node.kind)
    return threshold is not None and stages.index(stage) >= stages.index(threshold)


class IngestKanbanAdapter:
    def __init__(self, vault: str | Path, kanban: KanbanCLI | None = None,
                 *, enable_workers: bool = False):
        self.workflow = FileIngestWorkflowService(vault)
        self.kanban = kanban or KanbanCLI()
        self.enable_workers = enable_workers

    @staticmethod
    def _canary_allows(workflow: Mapping[str, Any], node: Node) -> bool:
        if node.kind == "source-prepare":
            return (workflow["batch_id"] is None and
                    workflow["current_stage"] in ("created", "source_preparing"))
        if node.kind == "exact-plan":
            coverage = FileIngestWorkflowService.source_coverage(workflow)
            return (workflow["batch_id"] is None and workflow["current_stage"] == "planning"
                    and not coverage["pending"] and bool(coverage["ready"]))
        policy = workflow.get("dispatch_policy", {})
        return (policy.get("mode") == "canary" and node.kind == "pass-slice"
                and node.name.partition(":")[2] in policy.get("slice_ids", []))

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        created = self.workflow.start(request)
        followup = {"workflow_id": created["workflow_id"],
                    "actor": created["actor"],
                    "expected_revision": created["revision"]}
        followup["input_digest"] = mutation_digest(followup)
        return self.sync(followup)

    def cancel(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = self.workflow.cancel(request)
        if value["batch_id"]:
            batch = self.workflow.knowledge._batch(value["batch_id"])
            if not batch.get("cancel_requested"):
                self.workflow.knowledge.cancel_batch(
                    value["batch_id"], value["actor"], batch["revision"])
        if value["kanban"]["board_id"]:
            for item in value["kanban"]["task_map"]:
                if "task_id" in item:
                    self.kanban.wait(value["kanban"]["board_id"], item["task_id"],
                                     "blocked", "Vault workflow cancelled")
        return {"workflow_id": value["workflow_id"], "state": "cancelled",
                "background_dispatch": False, "revision": value["revision"]}

    def resume(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = self.workflow.resume(request)
        if value["batch_id"]:
            batch = self.workflow.knowledge._batch(value["batch_id"])
            if batch.get("cancel_requested"):
                self.workflow.knowledge.resume_cancelled_batch(
                    value["batch_id"], value["actor"], batch["revision"])
        followup = {"workflow_id": value["workflow_id"], "actor": value["actor"],
                    "expected_revision": value["revision"]}
        followup["input_digest"] = mutation_digest(followup)
        return self.sync(followup)

    def arm_canary(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = self.workflow.arm_canary(request)
        followup = {"workflow_id": value["workflow_id"], "actor": value["actor"],
                    "expected_revision": value["revision"]}
        followup["input_digest"] = mutation_digest(followup)
        return self.sync(followup)

    def disarm_canary(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = self.workflow.disarm_canary(request)
        followup = {"workflow_id": value["workflow_id"], "actor": value["actor"],
                    "expected_revision": value["revision"]}
        followup["input_digest"] = mutation_digest(followup)
        return self.sync(followup)

    def sync(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.workflow._check_request(request)
        workflow_id = str(request["workflow_id"])
        lock = _vault_path(self.workflow.vault,
                           f"_system/ledgers/ingest-workflows/.dispatch-locks/{workflow_id}.lock")
        with _exclusive_lock(lock):
            return self._sync_locked(request)

    def _sync_locked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id, actor = str(request["workflow_id"]), str(request["actor"])
        value = self.workflow.status(workflow_id)
        if value["actor"] != actor:
            _fail("ACTOR_MISMATCH", "workflow actor differs")
        if value["revision"] != request["expected_revision"]:
            _fail("REVISION_CONFLICT", "workflow revision changed")
        if value["cancel_requested"]:
            _fail("WORKFLOW_STOPPED", "cancelled workflow cannot dispatch")
        templates = (self.workflow.pinned_templates(value) if self.enable_workers else {})
        if value["batch_id"]:
            batch = self.workflow.knowledge._batch(value["batch_id"])
            if not batch.get("slices_initialized"):
                self.workflow.knowledge.initialize_slices(
                    value["batch_id"], actor, batch["revision"])
            self.workflow.knowledge.reclaim_expired_slices(value["batch_id"])
            self.workflow.knowledge.refresh_ready_slices(value["batch_id"], actor)
        if not self.kanban.dispatcher_available():
            return {"workflow_id": workflow_id, "workflow_created": True,
                    "state": "dispatcher_unavailable", "background_dispatch": False,
                    "board_id": value["kanban"]["board_id"]}
        nodes = desired_graph(self.workflow, value)
        slug = board_slug(workflow_id)
        self.kanban.ensure_board(slug)
        ids: dict[str, str] = {}
        task_map: list[dict[str, str]] = []
        for node in nodes:
            worker_enabled = self.enable_workers and self._canary_allows(value, node)
            key = f"ingest:{workflow_id}:{node.kind}:{node.input_fingerprint}"
            pin = next((item for item in value.get("template_pins", [])
                        if item["kind"] == node.kind), None)
            slice_template_hash = (self.workflow.knowledge._slice(
                value["batch_id"], node.name.partition(":")[2])["template_hash"]
                if node.kind == "pass-slice" else None)
            body = json.dumps({"workflow_id": workflow_id, "node": node.name,
                               "kind": node.kind,
                               "input_fingerprint": node.input_fingerprint,
                               "vault": str(self.workflow.vault),
                               "worker_contract": "pinned-v1" if pin else "phase-7-pending",
                               "template_id": pin["template_id"] if pin else None,
                               "worker_template_hash": pin["template_hash"] if pin else None,
                               "slice_template_hash": slice_template_hash,
                               "instructions": templates.get(node.kind)},
                              ensure_ascii=False, sort_keys=True)
            task_id = self.kanban.create_node(
                slug, node, key, [ids[parent] for parent in node.parents], body,
                enable_workers=worker_enabled)
            ids[node.name] = task_id
            task_map.append({"node": node.name, "idempotency_key": key,
                             "task_id": task_id})
        value = self.workflow.status(workflow_id)
        current_keys = {item["idempotency_key"] for item in task_map}
        if value["kanban"]["board_id"] == slug:
            for previous in value["kanban"]["task_map"]:
                if (previous.get("task_id")
                        and previous["idempotency_key"] not in current_keys):
                    if previous["node"] == "exact-plan" and value["batch_id"] is not None:
                        self.kanban.complete(slug, previous["task_id"],
                                             "Authoritative Vault exact plan committed")
                    elif previous["node"].startswith("source-prepare:") and any(
                            previous["node"] == "source-prepare:" + fingerprint({
                                "path": item["path"], "content_sha256": item["content_sha256"]})[:16]
                            for item in value.get("source_outcomes", [])):
                        self.kanban.complete(slug, previous["task_id"],
                                             "Authoritative Vault source outcome committed")
                    else:
                        self.kanban.wait(slug, previous["task_id"], "blocked",
                                         "Superseded by Vault workflow graph")
        bind = {"workflow_id": workflow_id, "actor": actor,
                "expected_revision": value["revision"],
                "board_id": slug, "task_map": task_map}
        bind["input_digest"] = mutation_digest(bind)
        updated = self.workflow.bind_kanban(bind)
        cooldown = (self.workflow.knowledge._batch(updated["batch_id"]).get("cooldown_until")
                    if updated["batch_id"] else None)
        cooldown_active = (cooldown is not None and datetime.fromisoformat(
            cooldown.replace("Z", "+00:00")) > datetime.now(timezone.utc))
        for node in nodes:
            if domain_completed(self.workflow, updated, node):
                self.kanban.complete(slug, ids[node.name],
                                     "Authoritative Vault outcome already committed")
            elif node.gate or not (self.enable_workers and self._canary_allows(updated, node)):
                self.kanban.wait(slug, ids[node.name], "blocked",
                                 "Human checkpoint or outside armed canary")
            elif node.kind == "pass-slice":
                slice_id = node.name.partition(":")[2]
                state = self.workflow.knowledge._slice(updated["batch_id"], slice_id)["state"]
                if state == "retry_wait" or (state == "ready" and cooldown_active):
                    self.kanban.wait(slug, ids[node.name], "scheduled", "Vault retry backoff")
                elif state in ("blocked", "reconcile_required", "awaiting_approval",
                               "cancelled"):
                    self.kanban.wait(slug, ids[node.name], "blocked", "Vault worker gate")
                elif state == "ready":
                    self.kanban.unblock(slug, ids[node.name])
            elif node.kind in ("source-prepare", "exact-plan"):
                self.kanban.unblock(slug, ids[node.name])
        active_canary = self.enable_workers and any(
            not domain_completed(self.workflow, updated, node)
            and self._canary_allows(updated, node)
            and (node.kind != "pass-slice" or self.workflow.knowledge._slice(
                updated["batch_id"], node.name.partition(":")[2])["state"]
                in ("ready", "leased")) for node in nodes)
        return {"workflow_id": workflow_id, "workflow_created": True,
                "state": updated["state"], "background_dispatch": active_canary,
                "board_id": slug, "task_count": len(task_map)}

    def _slice_worker(self, workflow_id: str, node_name: str,
                      task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.enable_workers:
            _fail("WORKER_CONTRACT_UNAVAILABLE", "fixed worker templates are not installed")
        workflow = self.workflow.status(workflow_id)
        self.workflow.pinned_templates(workflow)
        policy = workflow.get("dispatch_policy", {})
        if (policy.get("mode") != "canary" or not node_name.startswith("pass-slice:")
                or node_name.partition(":")[2] not in policy.get("slice_ids", [])):
            _fail("ACCESS_DENIED", "Pass slice is outside the armed canary")
        if workflow["cancel_requested"] or workflow["current_stage"] != "analyzing":
            _fail("WORKFLOW_STOPPED", "Pass work is outside the active analyzing stage")
        matching = [item for item in workflow["kanban"]["task_map"]
                    if item["node"] == node_name and item.get("task_id") == task_id]
        if len(matching) != 1 or not node_name.startswith("pass-slice:"):
            _fail("ACCESS_DENIED", "Kanban task is not bound to this Pass slice")
        slice_id = node_name.partition(":")[2]
        value = self.workflow.knowledge._slice(workflow["batch_id"], slice_id)
        graph = desired_graph(self.workflow, workflow)
        node = next((item for item in graph if item.name == node_name), None)
        if node is None or matching[0]["idempotency_key"] != (
                f"ingest:{workflow_id}:{node.kind}:{node.input_fingerprint}"):
            _fail("STALE_INPUT", "Kanban node fingerprint changed")
        batch = self.workflow.knowledge._batch(workflow["batch_id"])
        if batch.get("cancel_requested"):
            _fail("WORKFLOW_STOPPED", "batch is cancelled")
        inputs = []
        for task_id in value["task_ids"]:
            task = self.workflow.knowledge._task(task_id)
            measured = self.workflow.knowledge._slice_task_input(batch, task, remeasure=True)
            if measured["blocking_code"]:
                _fail("STALE_INPUT", "slice task no longer fits its reading window")
            inputs.append({"task_id": task_id, "fingerprint": measured["fingerprint"]})
        if not value.get("reslice_count"):
            observed = "sha256:" + fingerprint({
                "batch_id": workflow["batch_id"], "task_inputs": inputs,
                "slice_config": batch["slice_config"],
                "template_id": value["template_id"],
                "template_hash": value["template_hash"],
            })
            if observed != value["input_fingerprint"]:
                _fail("STALE_INPUT", "slice input fingerprint changed")
        return workflow, value

    def _pre_worker(self, request: Mapping[str, Any], *, beginning: bool = False) -> tuple[dict[str, Any], Node, dict[str, Any] | None]:
        if not self.enable_workers:
            _fail("WORKER_CONTRACT_UNAVAILABLE", "fixed worker templates are not installed")
        workflow = self.workflow.status(str(request["workflow_id"]))
        self.workflow.pinned_templates(workflow)
        if workflow["cancel_requested"] or workflow["batch_id"] is not None:
            _fail("WORKFLOW_STOPPED", "source and plan work require an active pre-batch workflow")
        node_name = str(request["node"])
        node = next((item for item in desired_graph(self.workflow, workflow)
                     if item.name == node_name), None)
        if node is None or node.kind not in ("source-prepare", "exact-plan"):
            _fail("ACCESS_DENIED", "node is not a current pre-batch card")
        matching = [item for item in workflow["kanban"]["task_map"]
                    if item["node"] == node_name and item.get("task_id") == request["task_id"]]
        key = f"ingest:{workflow['workflow_id']}:{node.kind}:{node.input_fingerprint}"
        if len(matching) != 1 or matching[0]["idempotency_key"] != key:
            _fail("STALE_INPUT", "Kanban node binding changed")
        pin = next(item for item in workflow["template_pins"] if item["kind"] == node.kind)
        if (not beginning and request.get("template_hash") != pin["template_hash"]):
            _fail("STALE_INPUT", "worker template hash changed")
        source_info = None
        if node.kind == "source-prepare":
            for path in workflow["scope"]["source_paths"]:
                source = _vault_path(self.workflow.vault, path)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if node.name == "source-prepare:" + fingerprint({
                        "path": path, "content_sha256": digest})[:16]:
                    source_info = {"path": path, "content_sha256": digest}
                    break
            if source_info is None:
                _fail("STALE_INPUT", "source bytes changed")
        elif not self._canary_allows(workflow, node):
            _fail("INCOMPLETE_COVERAGE", "exact plan requires all source outcomes and a ready source")
        return workflow, node, source_info

    def worker_begin(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node, source = self._pre_worker(request, beginning=True)
            if node.kind == "source-prepare" and domain_completed(self.workflow, workflow, node):
                _fail("INVALID_TRANSITION", "source outcome is already recorded")
            pin = next(item for item in workflow["template_pins"] if item["kind"] == node.kind)
            return {"ok": True, "node": node.name, "template_hash": pin["template_hash"],
                    "input_fingerprint": node.input_fingerprint,
                    "source": source, "source_coverage": self.workflow.source_coverage(workflow)}
        workflow_id, node_name = str(request["workflow_id"]), str(request["node"])
        workflow, value = self._slice_worker(workflow_id, node_name,
                                             str(request["task_id"]))
        worker_id = str(request["worker_id"])
        result = self.workflow.knowledge.batch_next_slice(
            workflow["batch_id"], worker_id, slice_id=value["slice_id"])
        if not result["leased"]:
            return result
        self._slice_worker(workflow_id, node_name, str(request["task_id"]))
        return {"ok": True, "leased": True, "slice": result["slice"],
                "template_hash": result["slice"]["template_hash"],
                "task_ids": result["slice"]["task_ids"]}

    def worker_check(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node, source = self._pre_worker(request)
            return {"ok": True, "node": node.name, "source": source,
                    "source_coverage": self.workflow.source_coverage(workflow)}
        workflow, value = self._slice_worker(str(request["workflow_id"]),
                                             str(request["node"]),
                                             str(request["task_id"]))
        if (value["state"] != "leased"
                or value["lease"]["worker_id"] != request["worker_id"]
                or value["revision"] != request["expected_revision"]
                or value["template_hash"] != request["template_hash"]):
            _fail("STALE_INPUT", "slice lease, revision or template changed")
        expires = datetime.fromisoformat(value["lease"]["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(timezone.utc):
            _fail("LEASE_EXPIRED", "slice lease expired")
        return {"ok": True, "batch_id": workflow["batch_id"],
                "slice_id": value["slice_id"], "task_ids": value["task_ids"],
                "input_fingerprint": value["input_fingerprint"]}

    def worker_heartbeat(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            checked = self.worker_check(request)
            workflow = self.workflow.status(str(request["workflow_id"]))
            self.kanban.command("kanban", "--board", workflow["kanban"]["board_id"],
                                "heartbeat", str(request["task_id"]))
            return checked
        checked = self.worker_check(request)
        result = self.workflow.knowledge.slice_heartbeat(
            checked["batch_id"], checked["slice_id"],
            str(request["worker_id"]), int(request["expected_revision"]))
        workflow = self.workflow.status(str(request["workflow_id"]))
        self.kanban.command("kanban", "--board", workflow["kanban"]["board_id"],
                            "heartbeat", str(request["task_id"]))
        return result

    def worker_complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node, source = self._pre_worker(request)
            if node.kind == "source-prepare":
                outcome = {"workflow_id": workflow["workflow_id"], "actor": workflow["actor"],
                           "expected_revision": workflow["revision"], **source,
                           "status": "ready", "resource_id": request["resource_id"],
                           "unit_set_id": request["unit_set_id"],
                           "artifact_refs": request.get("artifact_refs", [])}
                outcome["input_digest"] = mutation_digest(outcome)
                updated = self.workflow.record_source_outcome(outcome)
                result = {"ok": True, "source_coverage": self.workflow.source_coverage(updated)}
            else:
                transition = {"workflow_id": workflow["workflow_id"], "actor": workflow["actor"],
                              "expected_revision": workflow["revision"], "target_stage": "analyzing",
                              "batch_id": request["batch_id"]}
                transition["input_digest"] = mutation_digest(transition)
                updated = self.workflow.reconcile(transition)
                result = {"ok": True, "batch_id": updated["batch_id"],
                          "source_coverage": self.workflow.source_coverage(updated)}
            self.kanban.complete(workflow["kanban"]["board_id"], str(request["task_id"]),
                                 "Authoritative Vault pre-batch outcome committed")
            sync_request = {"workflow_id": updated["workflow_id"], "actor": updated["actor"],
                            "expected_revision": updated["revision"]}
            sync_request["input_digest"] = mutation_digest(sync_request)
            result["dispatch"] = self.sync(sync_request)
            return result
        workflow_id, node_name = str(request["workflow_id"]), str(request["node"])
        workflow, value = self._slice_worker(workflow_id, node_name,
                                             str(request["task_id"]))
        result_refs = []
        for task_id in value["task_ids"]:
            root = _vault_path(self.workflow.vault,
                               f"_system/knowledge-builds/task-{task_id}/passes")
            records = []
            for path in sorted(root.glob("*.json")) if root.exists() else []:
                record = _load_json(path)
                validate_record("knowledge_pass", record)
                records.append((path, record))
            sequences = {record["sequence"] for _, record in records}
            if (0 not in sequences or not any(sequence > 0 for sequence in sequences)
                    or sequences != set(range(max(sequences) + 1))):
                _fail("INCOMPLETE_COVERAGE", "each slice task needs candidate and citation Passes")
            result_refs.extend(path.relative_to(self.workflow.vault).as_posix()
                               for path, _ in records)
        if value["state"] == "completed":
            if sorted(value["result_refs"]) != sorted(result_refs):
                _fail("IDEMPOTENCY_CONFLICT", "completed slice Pass set changed")
        else:
            self.worker_check(request)
            self.workflow.knowledge.slice_complete({
                "batch_id": workflow["batch_id"], "slice_id": value["slice_id"],
                "worker_id": request["worker_id"],
                "expected_revision": request["expected_revision"],
                "result_refs": result_refs,
            })
        self.kanban.complete(workflow["kanban"]["board_id"],
                             str(request["task_id"]), "Vault Pass slice complete")
        return {"ok": True, "slice_id": value["slice_id"],
                "result_refs": result_refs}

    def worker_fail(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node, source = self._pre_worker(request)
            if node.kind == "exact-plan":
                self.kanban.wait(workflow["kanban"]["board_id"], str(request["task_id"]),
                                 "blocked", str(request["message"]))
                return {"ok": False, "error_code": request["code"],
                        "source_coverage": self.workflow.source_coverage(workflow)}
            outcome = {"workflow_id": workflow["workflow_id"], "actor": workflow["actor"],
                       "expected_revision": workflow["revision"], **source,
                       "status": "failed", "error_code": request["code"],
                       "reason": request["message"],
                       "artifact_refs": request.get("artifact_refs", [])}
            outcome["input_digest"] = mutation_digest(outcome)
            updated = self.workflow.record_source_outcome(outcome)
            self.kanban.complete(workflow["kanban"]["board_id"], str(request["task_id"]),
                                 "Source failed; coverage gap recorded")
            sync_request = {"workflow_id": updated["workflow_id"], "actor": updated["actor"],
                            "expected_revision": updated["revision"]}
            sync_request["input_digest"] = mutation_digest(sync_request)
            return {"ok": True, "source_coverage": self.workflow.source_coverage(updated),
                    "dispatch": self.sync(sync_request)}
        checked = self.worker_check(request)
        outcome = self.workflow.knowledge.slice_fail({
            "batch_id": checked["batch_id"], "slice_id": checked["slice_id"],
            "worker_id": request["worker_id"],
            "expected_revision": request["expected_revision"],
            "code": request["code"], "message": request["message"],
            **({"failed_output": request["failed_output"]}
               if "failed_output" in request else {}),
        })
        workflow = self.workflow.status(str(request["workflow_id"]))
        state = outcome["slice"]["state"]
        self.kanban.wait(workflow["kanban"]["board_id"], str(request["task_id"]),
                         "scheduled" if state == "retry_wait" else "blocked",
                         f"Vault slice state: {state}")
        return outcome
