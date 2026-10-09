"""Rebuildable Hermes Kanban projection of a Vault ingest workflow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

from hermes_source_units import (ContractError, FileIngestWorkflowService,
                                 mutation_digest)
from hermes_source_units.source_units import (_exclusive_lock, _json_bytes, _load_json,
                                              _vault_path, _write_atomic)
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.ingest_workflow import SOURCE_FAILURE_CODES
from hermes_source_units.workflow_guard import workflow_write_guard, worker_binding, WORKER_LOCK_TIMEOUT


def _fail(code: str, message: str) -> None:
    raise ContractError(code, "$", message)


PROGRAM_ASSIGNEE = 'ingest-program'
PROGRAM_KINDS = frozenset(('source-prepare', 'exact-plan', 'checkpoint-1-validate',
    'vault-finalize-plan', 'checkpoint-2-validate', 'release-apply', 'provider-sync', 'acceptance'))


def program_template(kind, content):
    return kind in PROGRAM_KINDS and '<!-- hermes-program-worker/v1 -->' in (content or '')


SEMANTIC_KINDS = frozenset(('pass-slice', 'resource-reduce', 'global-reduce'))


def semantic_template(kind, content):
    return kind in SEMANTIC_KINDS and '<!-- hermes-fixed-semantic/v1 -->' in (content or '')


def executor_for(kind, content):
    return 'program-v1' if program_template(kind, content) else 'semantic-v1' if semantic_template(kind, content) else 'model'


@dataclass(frozen=True)
class Node:
    name: str
    kind: str
    parents: tuple[str, ...]
    input_fingerprint: str
    gate: bool = False


def node_identity(workflow, kind, inputs, parent_keys):
    identity = {'workflow_id':workflow['workflow_id'], 'kind':kind,
                'inputs':inputs, 'parent_keys':parent_keys}
    if workflow.get('repair_history'):
        identity['repair_id'] = workflow['repair_history'][-1]['repair_id']
    pin = next((p for p in workflow.get('template_pins', []) if p['kind']==kind), None)
    if pin:
        identity['worker_template_hash'] = pin['template_hash']
    return 'sha256:' + fingerprint(identity)


def pass_node(workflow, value):
    revision = next((r for r in reversed([*workflow.get('pass_revision_history', []),
        *([workflow['pass_revision']] if workflow.get('pass_revision') else [])])
        if value['slice_id'] in r['slice_ids']), None)
    return Node('pass-slice:' + value['slice_id'], 'pass-slice', (),
                node_identity(workflow, 'pass-slice', {
                    'slice_id':value['slice_id'], 'input_fingerprint':value['input_fingerprint'],
                    'template_hash':value['template_hash'],
                    **({'revision_digest': revision['input_digest']} if revision else {})}, []))


def desired_graph(service: FileIngestWorkflowService, workflow: Mapping[str, Any]) -> list[Node]:
    """Derive the DAG from Vault records, never from Kanban's task state."""
    nodes: list[Node] = []
    workflow_id = workflow["workflow_id"]
    def add(name: str, kind: str, parents: tuple[str, ...], inputs: Any,
            gate: bool = False) -> str:
        parents_fingerprints = [next(item.input_fingerprint for item in nodes if item.name == parent)
                               for parent in parents]
        nodes.append(Node(name, kind, parents, node_identity(workflow, kind, inputs, parents_fingerprints), gate))
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
    if workflow.get('pass_revision') and workflow['pass_revision']['state'] == 'running':
        return [pass_node(workflow, service.knowledge._slice(batch_id, sid))
                for sid in workflow['pass_revision']['slice_ids']]
    staged = bool(workflow['scope'].get('pause_after'))
    if staged and service.pending_pause(workflow) == 'exact_plan':
        return []  # Slice manifests are the plan; unopened phases need no native cards.
    if ((workflow['scope'].get('execution_mode') == 'canary_only' or staged)
            and workflow['dispatch_policy']['mode'] == 'canary'):
        return [pass_node(workflow, service.knowledge._slice(batch_id, sid))
                for sid in workflow['dispatch_policy']['slice_ids']]
    slices = service.knowledge._slices(batch_id)
    pass_nodes: dict[str, str] = {}
    for item in slices:
        name = f"pass-slice:{item['slice_id']}"
        nodes.append(pass_node(workflow, item))
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
    if staged:
        last_kind = {"analyzing": "pass-slice", "reducing": "checkpoint-1-validate",
                     "checkpoint_1": "checkpoint-1-gate", "build_finalizing": "build-finalize",
                     "release_planning": "checkpoint-2-validate", "checkpoint_2": "checkpoint-2-gate",
                     "applying": "release-apply", "indexing": "provider-sync" if provider == "sync" else "release-apply",
                     "validating": "acceptance", "completed": "acceptance"}[workflow["current_stage"]]
        eligible_prefix = [i for i, node in enumerate(nodes) if node.kind == last_kind]
        nodes = nodes[:max(eligible_prefix) + 1] if eligible_prefix else []
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
        if (os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT")
                or os.environ.get("HERMES_KANBAN_TASK")):
            _fail("ACCESS_DENIED", "Kanban reconciliation requires a trusted orchestrator process")
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
        executor = json.loads(body).get('executor')
        argv = ["kanban", "--board", slug, "create", node.name,
                "--body", body, "--idempotency-key", key,
                "--created-by", "ingest-workflow", "--assignee", PROGRAM_ASSIGNEE if executor in ('program-v1', 'semantic-v1') else "default",
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
        if self.task_status(slug, task_id) in ("done", "archived"):
            return
        self.command("kanban", "--board", slug, "complete", task_id,
                     "--force", "--result", result)

    def task_status(self, slug: str, task_id: str) -> str:
        shown = json.loads(self.command("kanban", "--board", slug, "show",
                                        task_id, "--json"))
        return str(shown["task"]["status"])

    def task_snapshot(self, slug: str, task_id: str) -> dict[str, Any]:
        return json.loads(self.command("kanban", "--board", slug, "show", task_id, "--json"))

    def task_states(self, slug: str) -> dict[str, str]:
        result = json.loads(self.command('kanban', '--board', slug, 'list', '--json'))
        rows = result.get('tasks', []) if isinstance(result, dict) else result
        return {row['id']: row['status'] for row in rows}

    def task_log(self, slug: str, task_id: str) -> str:
        return self.command("kanban", "--board", slug, "log", task_id, "--tail", "65536")

    def archive(self, slug: str, task_id: str) -> None:
        if self.task_status(slug, task_id) != "archived":
            # Closing a parent can promote native/decomposed children. Close the
            # whole dependent subtree first, including cards absent from Vault.
            for child in self.task_snapshot(slug, task_id).get('children', []):
                child_id = child.get('id', child.get('task_id')) if isinstance(child, dict) else child
                if child_id:
                    self.archive(slug, child_id)
            self.command("kanban", "--board", slug, "archive", task_id)

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
        return workflow["current_stage"] in ("checkpoint_1", "build_finalizing",
                                               "release_planning", "checkpoint_2",
                                               "applying", "indexing", "validating", "completed")
    if node.kind == "checkpoint-1-gate":
        checkpoint = workflow["checkpoints"]["checkpoint_1"]
        if checkpoint.get("decision_ref") and hashlib.sha256(_vault_path(
                service.vault, checkpoint["decision_ref"]).read_bytes()).hexdigest() != checkpoint.get("decision_sha256"):
            _fail("SOURCE_CHANGED", "checkpoint 1 decision evidence changed")
        return checkpoint["state"] == "approved"
    if node.kind == "checkpoint-2-gate":
        checkpoint = workflow["checkpoints"]["checkpoint_2"]
        if checkpoint.get("decision_ref"):
            decision_path = _vault_path(service.vault, checkpoint["decision_ref"])
            if hashlib.sha256(decision_path.read_bytes()).hexdigest() != checkpoint.get("decision_sha256"):
                _fail("SOURCE_CHANGED", "checkpoint 2 decision evidence changed")
            decision = _load_json(decision_path)
            lint_ref = decision.get("evidence", {}).get("lint_ref")
            if not lint_ref or _load_json(_vault_path(service.vault, lint_ref)) != decision.get("lint"):
                _fail("SOURCE_CHANGED", "checkpoint 2 lint evidence changed")
        return checkpoint["state"] == "approved"
    if node.kind == "build-finalize":
        status = service.knowledge.batch_status(workflow["batch_id"], compact=True)
        return bool(status["runs"]) and all(item["state"] == "completed"
                                                for item in status["runs"])
    if node.kind == "vault-finalize-plan":
        return bool(workflow.get("release_id") and workflow.get("release_plan_id"))
    stages = ("created", "source_preparing", "planning", "analyzing", "reducing",
              "checkpoint_1", "build_finalizing", "release_planning", "checkpoint_2",
              "applying", "indexing", "validating", "completed")
    stage = workflow["current_stage"]
    thresholds = {"source-prepare": "planning", "exact-plan": "analyzing",
                  "checkpoint-2-validate": "checkpoint_2",
                  "release-apply": "indexing", "provider-sync": "validating",
                  "acceptance": "completed"}
    threshold = thresholds.get(node.kind)
    return threshold is not None and stages.index(stage) >= stages.index(threshold)


def _runtime_error_category(error: str, outcome: str | None) -> str | None:
    if re.fullmatch(r"pid \d+ exited rate-limited \(quota wall\).*", error, re.S):
        # This native sentence is synthesized solely from rc=75, including
        # APIConnectionError exits; it is not provider evidence.
        return "provider_retry_reason_unknown"
    category = None
    if re.search(r"APIConnectionError|ConnectError|connection (?:error|failed)|connection reset", error, re.I):
        category = "model_connection_failed"
    elif re.search(r"insufficient_quota|quota (?:exceeded|exhausted)", error, re.I):
        category = "quota_exhausted"
    elif re.search(r"RateLimitError|HTTP\s*429|rate.?limit", error, re.I):
        category = "model_rate_limited"
    elif error:
        category = "runtime_error"
    elif outcome == "rate_limited":
        category = "provider_retry_reason_unknown"
    return category


def runtime_log_observations(log: str) -> dict[str, Any]:
    """Bounded diagnostic hints, never assign an unbound log to a current run."""
    categories = set()
    for line in log[-65536:].splitlines():
        if re.search(r"(?:API|provider|model).*(?:request failed|call failed|error_type=|exception chain)", line, re.I):
            category = _runtime_error_category(line, None)
            if category in ("model_connection_failed", "model_rate_limited", "quota_exhausted"):
                categories.add(category)
    return {"scope": "recent task log tail; not correlated to an attempt",
            "error_categories": sorted(categories), "current_attempt_proven": False}


def runtime_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only attempt telemetry; exit 75 alone does not prove quota exhaustion."""
    task = snapshot["task"]
    runs = snapshot.get("runs", [])
    latest = max(runs, key=lambda run: int(run["id"]), default=None)
    error = str((latest or {}).get("error") or "")
    category = _runtime_error_category(error, (latest or {}).get("outcome"))
    failed = max((run for run in runs if run.get("ended_at") is not None and
                  run.get("outcome") in ("rate_limited", "crashed", "spawn_failed")),
                 key=lambda run: int(run["id"]), default=None)
    history = None if failed is None else {
        "attempt_id": failed["id"], "ended_at": failed["ended_at"],
        "error_category": _runtime_error_category(str(failed.get("error") or ""), failed.get("outcome")),
        "error_excerpt": str(failed.get("error") or "")[:500] or None}
    status = str(task["status"])
    active = bool(latest and latest.get("ended_at") is None)
    state = status
    if status in ("ready", "todo", "scheduled") and not active and latest:
        if latest.get("outcome") in ("rate_limited", "crashed", "spawn_failed"):
            state = "retry_wait"
    # Previous failed attempts remain history once a newer run has started.
    return {"kanban_status": status, "state": state,
            "attempt_id": (latest or {}).get("id"),
            "attempt_outcome": (latest or {}).get("outcome"),
            "attempt_started_at": (latest or {}).get("started_at"),
            "attempt_ended_at": (latest or {}).get("ended_at"),
            "error_category": category, "error_excerpt": error[:500] or None,
            "last_failed_attempt": history,
            "retry_not_before": None,
            "retry_time_note": "Native task snapshot does not expose a retry deadline; retry_wait means requeued, not a guaranteed retry time."}


class IngestKanbanAdapter:
    def __init__(self, vault: str | Path, kanban: KanbanCLI | None = None,
                 *, enable_workers: bool = False):
        self.workflow = FileIngestWorkflowService(vault)
        self.kanban = kanban or KanbanCLI()
        self.enable_workers = enable_workers
        config = _load_json(Path(__file__).resolve().parents[2] /
                            "hermes-obsidian-governed-ingest-orchestrator/config/orchestration.json")
        self.source_prepare_concurrency = int(config.get("source_prepare_concurrency", 2))
        if self.source_prepare_concurrency < 1:
            _fail("INVALID_SCHEMA", "source_prepare_concurrency must be positive")

    @staticmethod
    def _mutation(value: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
        request = {"workflow_id": value["workflow_id"], "actor": value["actor"],
                   "expected_revision": value["revision"], **fields}
        request["input_digest"] = mutation_digest(request)
        return request

    def _advance(self, value: Mapping[str, Any], target: str,
                 refs: list[str] | None = None) -> dict[str, Any]:
        return self.workflow.reconcile(self._mutation(
            value, target_stage=target, artifact_refs=refs or []))

    def runtime_status(self, workflow_id: str) -> dict[str, Any]:
        """Observe native cards without dispatching or writing the Vault ledger."""
        value = self.workflow.status(workflow_id)
        cards = []
        for binding in value["kanban"]["task_map"]:
            card = {"node": binding["node"], "task_id": binding["task_id"]}
            try:
                snapshot = self.kanban.task_snapshot(value["kanban"]["board_id"], binding["task_id"])
                if snapshot["task"]["id"] != binding["task_id"]:
                    _fail("KANBAN_INVALID_RESPONSE", "snapshot names another card")
                card.update(runtime_projection(snapshot))
                if card["state"] in ("running", "retry_wait", "blocked") and hasattr(self.kanban, "task_log"):
                    try:
                        card["log_observations"] = runtime_log_observations(
                            self.kanban.task_log(value["kanban"]["board_id"], binding["task_id"]))
                    except (ContractError, OSError, ValueError) as exc:
                        card["log_observation_error"] = (exc.code if isinstance(exc, ContractError)
                                                         else "KANBAN_UNAVAILABLE")
            except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
                card.update(state="unknown", observation_error=(
                    exc.code if isinstance(exc, ContractError) else "KANBAN_UNAVAILABLE"))
            report_path = _vault_path(self.workflow.vault,
                f"_system/ledgers/ingest-workflows/{workflow_id}/reports/failed-{fingerprint(binding['node'])[:16]}.json")
            if report_path.is_file():
                report = _load_json(report_path)
                completed_elsewhere = (binding['node'].startswith('pass-slice:') and value['batch_id']
                    and self.workflow.knowledge._slice(value['batch_id'], binding['node'].partition(':')[2])['state'] == 'completed'
                    and report.get('task_id') != binding['task_id'])
                if completed_elsewhere:
                    card['historical_projection_report_ref'] = report_path.relative_to(self.workflow.vault).as_posix()
                elif binding['idempotency_key'].endswith(':' + report.get('input_fingerprint', 'INVALID')):
                    card.update(native_state=card['state'], state=('execution_blocked' if report.get('error_code') else 'validation_blocked'),
                        error_code=report.get('error_code'), reason=report.get('reason'),
                        report_ref=report_path.relative_to(self.workflow.vault).as_posix())
            cards.append(card)
        return {"observed_at": datetime.now(timezone.utc).isoformat(),
                "authority": "native Kanban attempt telemetry; source coverage remains Vault-authoritative",
                "cards": cards}

    def _sync_current(self, workflow_id: str) -> dict[str, Any]:
        for _ in range(16):
            value = self.workflow.status(workflow_id)
            try:
                return self.sync(self._mutation(value))
            except ContractError as exc:
                if exc.code not in ("REVISION_CONFLICT", "LOCK_BUSY"):
                    raise
                if _ == 15:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _pending_reconciliation(result: dict[str, Any]) -> dict[str, Any]:
        result["kanban_reconciliation_pending"] = True
        return result

    def _failed_card(self, workflow_id: str, node: Node) -> bool:
        path = _vault_path(self.workflow.vault,
            f"_system/ledgers/ingest-workflows/{workflow_id}/reports/"
            f"failed-{fingerprint(node.name)[:16]}.json")
        if not path.is_file():
            return False
        report = _load_json(path)
        if report.get('input_fingerprint') != node.input_fingerprint:
            return False
        value = self.workflow.status(workflow_id)
        current = next((c for c in value['kanban']['task_map'] if c['node'] == node.name), None)
        return not (node.kind == 'pass-slice' and domain_completed(self.workflow, value, node)
                    and current and report.get('task_id') != current['task_id'])

    def _completed_pass_binding(self, value, node, key):
        """Reuse a verified completed native identity, including an archived one."""
        if node.kind != 'pass-slice' or not domain_completed(self.workflow, value, node):
            return None
        path = _vault_path(self.workflow.vault,
            f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(key)}.json")
        if not path.is_file():
            return None
        try:
            record = _load_json(path)
            task_id = record['task_id']
            expected = {'workflow_id':value['workflow_id'], 'node':node.name, 'task_id':task_id,
                'actor':value['actor'], 'input_fingerprint':node.input_fingerprint,
                'template_hash':self.workflow.knowledge._slice(value['batch_id'], node.name.partition(':')[2])['template_hash'],
                'worker_id':'ingest-worker-' + task_id}
            if record != expected:
                return None
            current = next((c for c in value['kanban']['task_map'] if c['node']==node.name and c['idempotency_key']==key), None)
            if current and current['task_id']==task_id:
                return task_id
            snapshot = self.kanban.task_snapshot(value['kanban']['board_id'], task_id)
            native = snapshot['task']; body = json.loads(native['body'])
            if (native['id']!=task_id or native['status'] not in ('done','archived')
                    or native.get('created_by')!='ingest-workflow'
                    or any(body.get(k)!=v for k,v in {'workflow_id':value['workflow_id'], 'node':node.name,
                        'input_fingerprint':node.input_fingerprint,'vault':str(self.workflow.vault),
                        'worker_binding':str(path),'slice_template_hash':expected['template_hash']}.items())):
                return None
            if self.workflow.pending_pause(value):
                self.workflow._verify_pause_snapshot(value)
            self._report(value, 'projection-recovery-' + fingerprint(key)[:16], {
                'node':node.name,'input_fingerprint':node.input_fingerprint,'restored_task_id':task_id,
                'previous_task_id':current['task_id'] if current else None,
                'binding_ref':path.relative_to(self.workflow.vault).as_posix(),
                'binding_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'domain_outcome':'completed','native_state':native['status'],'rerun':False})
            return task_id
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            return None

    def _execution_blocked(self, workflow_id: str, node: Node) -> bool:
        path = _vault_path(self.workflow.vault,
            f"_system/ledgers/ingest-workflows/{workflow_id}/reports/failed-{fingerprint(node.name)[:16]}.json")
        return (self._failed_card(workflow_id, node)
                and bool(_load_json(path).get('error_code')))

    def execution_failure(self, request: Mapping[str, Any], code: str, message: str) -> str:
        """Record execution failure from a verified current dispatcher binding."""
        value = self.workflow.status(str(request["workflow_id"]))
        node = next((n for n in desired_graph(self.workflow, value)
                     if n.name == request["node"]), None)
        if node is None or not any(c.get("task_id") == request["task_id"] and
                c["idempotency_key"] == f"ingest:{value['workflow_id']}:{node.kind}:{node.input_fingerprint}"
                for c in value["kanban"]["task_map"]):
            _fail("STALE_INPUT", "failure reporter has no current dispatcher binding")
        return self._report(value, f"failed-{fingerprint(node.name)[:16]}", {
            "node": node.name, "task_id": request["task_id"],
            "input_fingerprint": node.input_fingerprint,
            "idempotency_key": f"ingest:{value['workflow_id']}:{node.kind}:{node.input_fingerprint}",
            "error_code": code, "reason": message, "source_failed": False,
            "state": "execution_blocked"})

    def _projection_failure(self, workflow_id: str, node: Node, task_id: str,
                            code: str, message: str) -> str:
        """Retry a changed trusted projection; keep worker failure reports strict."""
        with _exclusive_lock(self.workflow._lock(workflow_id)):
            current = self.workflow.status(workflow_id)
            latest = next((item for item in desired_graph(self.workflow, current)
                           if item.name == node.name), None)
            if latest is None or latest.input_fingerprint != node.input_fingerprint:
                _fail('REVISION_CONFLICT', 'domain inputs changed during failure projection')
            return self.execution_failure({'workflow_id':workflow_id, 'node':node.name,
                                           'task_id':task_id}, code, message)

    @staticmethod
    def validate_worker_request(request: Mapping[str, Any], *, beginning: bool = False) -> None:
        required = ("workflow_id", "node", "task_id") + (() if beginning else ("template_hash",))
        if "worker_template_hash" in request:
            _fail("INVALID_SCHEMA", "use canonical template_hash; worker_template_hash is not a request field")
        missing = [k for k in required if not isinstance(request.get(k), str) or not request[k]]
        if missing:
            _fail("INVALID_SCHEMA", "missing worker request fields: " + ", ".join(missing))

    def _canary_allows(self, workflow: Mapping[str, Any], node: Node, *, completing: bool = False) -> bool:
        if self.workflow.revision_allows(workflow, node.name):
            return True
        if not completing and self.workflow.pending_pause(workflow):
            return False
        if node.kind == "source-prepare":
            return (workflow["batch_id"] is None and
                    workflow["current_stage"] in ("created", "source_preparing"))
        if node.kind == "exact-plan":
            coverage = FileIngestWorkflowService.source_coverage(workflow)
            return (workflow["batch_id"] is None and workflow["current_stage"] == "planning"
                    and not coverage["pending"] and bool(coverage["ready"]))
        policy = workflow.get("dispatch_policy", {})
        if node.kind == "pass-slice" and policy.get("mode") == "full":
            return workflow["current_stage"] in ("analyzing", "reducing")
        if (workflow["scope"].get("execution_mode") == "auto_full"
                and policy.get("mode") == "full"):
            stages = {"resource-reduce": "reducing", "global-reduce": "reducing",
                      "checkpoint-1-validate": "reducing",
                      "build-finalize": "build_finalizing",
                      "vault-finalize-plan": "release_planning",
                      "checkpoint-2-validate": "release_planning",
                      "release-apply": "applying", "provider-sync": "indexing",
                      "acceptance": "validating"}
            return workflow["current_stage"] == stages.get(node.kind)
        return (policy.get("mode") == "canary" and node.kind == "pass-slice"
                and node.name.partition(":")[2] in policy.get("slice_ids", []))

    def _eligible(self, workflow: Mapping[str, Any], node: Node,
                  nodes: list[Node]) -> bool:
        by_name = {item.name: item for item in nodes}
        return (self.enable_workers and self._canary_allows(workflow, node)
                and all(domain_completed(self.workflow, workflow, by_name[parent])
                        for parent in node.parents))

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

    def amend_execution(self, request, *, dry_run=False):
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            _fail('ACCESS_DENIED', 'execution amendment requires a trusted operator')
        self.workflow._check_request(request)
        wid = str(request['workflow_id'])
        lock = _vault_path(self.workflow.vault, f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
        with _exclusive_lock(lock):
            value = self.workflow.status(wid)
            if hasattr(self.kanban, 'task_states') and value['kanban']['board_id']:
                states = self.kanban.task_states(value['kanban']['board_id'])
                if any(states.get(c.get('task_id')) in ('running','ready','scheduled','todo')
                       for c in value['kanban']['task_map']):
                    _fail('WORKER_ACTIVE', 'native consumers must stop before execution amendment')
            if hasattr(self.kanban, 'task_snapshot') and value['kanban']['board_id']:
                for card in value['kanban']['task_map']:
                    snapshot = self.kanban.task_snapshot(value['kanban']['board_id'], card['task_id'])
                    if snapshot.get('task', {}).get('status') in ('running','ready','scheduled','todo'):
                        _fail('WORKER_ACTIVE', 'native card remains dispatchable before execution amendment')
                    if any(run.get('status') == 'running' for run in snapshot.get('runs', [])):
                        _fail('WORKER_ACTIVE', 'native run must finish before execution amendment')
            return self.workflow.amend_execution(request, dry_run=dry_run)

    def continue_workflow(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = self.workflow.continue_workflow(request)
        return self.sync(self._mutation(value))

    def revise_passes(self, request):
        value = self.workflow.revise_passes(request)
        return self.sync(self._mutation(value))

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
        if (os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT")
                or os.environ.get("HERMES_KANBAN_TASK")):
            _fail("ACCESS_DENIED", "Kanban reconciliation requires a trusted orchestrator process")
        self.workflow._check_request(request)
        workflow_id = str(request["workflow_id"])
        lock = _vault_path(self.workflow.vault,
                           f"_system/ledgers/ingest-workflows/.dispatch-locks/{workflow_id}.lock")
        with _exclusive_lock(lock):
            return self._sync_locked(request)

    def _sync_locked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id, actor = str(request["workflow_id"]), str(request["actor"])
        value = self.workflow.status(workflow_id)
        self.workflow.assert_execution_ready(value)
        if value["actor"] != actor:
            _fail("ACTOR_MISMATCH", "workflow actor differs")
        if value["revision"] != request["expected_revision"]:
            _fail("REVISION_CONFLICT", "workflow revision changed")
        if value["cancel_requested"]:
            _fail("WORKFLOW_STOPPED", "cancelled workflow cannot dispatch")
        templates = (self.workflow.pinned_templates(value) if self.enable_workers else {})
        if value['batch_id'] is None and value['current_stage'] in ('created', 'source_preparing'):
            coverage = self.workflow.source_coverage(value)
            if not coverage['pending'] and coverage['ready']:
                value = self._advance(value, 'planning')
        if value["batch_id"]:
            batch = self.workflow.knowledge._batch(value["batch_id"])
            if not batch.get("slices_initialized"):
                self.workflow.knowledge.initialize_slices(
                    value["batch_id"], actor, batch["revision"])
            self.workflow.prepare_pass_revision(value)
            value = self.workflow.finish_pass_revision(self._mutation(value))
            value = self.workflow.latch_pause(self._mutation(value))
            if not self.workflow.pending_pause(value) or (value.get('pass_revision') and value['pass_revision']['state'] == 'running'):
                self.workflow.knowledge.reclaim_expired_slices(value["batch_id"])
                self.workflow.knowledge.refresh_ready_slices(value["batch_id"], actor)
            if (value["scope"].get("execution_mode") in ("canary_only", "auto_full")
                    and not self.workflow.pending_pause(value)
                    and value["current_stage"] == "analyzing"):
                policy = value["dispatch_policy"]
                if policy["mode"] == "disabled":
                    try:
                        preview = self.workflow.canary_preview(workflow_id, 8)
                    except ContractError as exc:
                        if exc.code != "CANARY_UNAVAILABLE":
                            raise
                        return {"workflow_id": workflow_id, "workflow_created": True,
                                "state": "canary_unavailable", "reason": str(exc),
                                "background_dispatch": False,
                                "board_id": value["kanban"]["board_id"]}
                    armed = {"workflow_id": workflow_id, "actor": actor,
                             "expected_revision": value["revision"],
                             "slice_ids": [item["slice_id"] for item in preview["slices"]],
                             "selection_digest": preview["selection_digest"]}
                    armed["input_digest"] = mutation_digest(armed)
                    value = self.workflow.arm_canary(armed)
                elif value["scope"].get("execution_mode") == "auto_full" and policy["mode"] == "canary" and len(policy["slice_ids"]) == 8 and all(
                        self.workflow.knowledge._slice(value["batch_id"], slice_id)["state"] == "completed"
                        for slice_id in policy["slice_ids"]):
                    promoted = {"workflow_id": workflow_id, "actor": actor,
                                "expected_revision": value["revision"]}
                    promoted["input_digest"] = mutation_digest(promoted)
                    value = self.workflow.promote_canary(promoted)
                if (value["dispatch_policy"]["mode"] == "full"
                        and not self.workflow.pending_pause(value)
                        and value["current_stage"] == "analyzing"
                        and all(item["state"] == "completed"
                                for item in self.workflow.knowledge._slices(value["batch_id"]))):
                    transition = {"workflow_id": workflow_id, "actor": actor,
                                  "expected_revision": value["revision"],
                                  "target_stage": "reducing"}
                    transition["input_digest"] = mutation_digest(transition)
                    value = self.workflow.reconcile(transition)
            if (value["scope"].get("execution_mode") == "auto_full"
                    and not self.workflow.pending_pause(value)
                    and value["dispatch_policy"]["mode"] == "full"):
                if (value["current_stage"] == "checkpoint_1"
                        and value["checkpoints"]["checkpoint_1"]["state"] == "approved"):
                    value = self._advance(value, "build_finalizing")
                if value["current_stage"] == "build_finalizing":
                    status = self.workflow.knowledge.batch_status(value["batch_id"], compact=True)
                    if status["runs"] and all(item["state"] == "completed" for item in status["runs"]):
                        refs = [f"_system/knowledge-builds/{item['run_id']}/manifest.json"
                                for item in status["runs"]]
                        value = self._advance(value, "release_planning", refs)
                if (value["current_stage"] == "checkpoint_2"
                        and value["checkpoints"]["checkpoint_2"]["state"] == "approved"):
                    plan_ref = (f"_system/knowledge-releases/{value['release_id']}/plan.json")
                    value = self._advance(value, "applying", [plan_ref])
                if (value["current_stage"] == "indexing"
                        and value["scope"].get("provider", "skip") == "skip"):
                    manifest_ref = (f"_system/knowledge-releases/{value['release_id']}/manifest.json")
                    value = self._advance(value, "validating", [manifest_ref])
        if not self.kanban.dispatcher_available():
            return {"workflow_id": workflow_id, "workflow_created": True,
                    "state": "dispatcher_unavailable", "background_dispatch": False,
                    "board_id": value["kanban"]["board_id"]}
        nodes = desired_graph(self.workflow, value)
        if (value['batch_id'] and value['scope'].get('execution_mode') == 'canary_only'
                and value['dispatch_policy']['mode'] == 'canary'):
            selected = {'pass-slice:' + sid for sid in value['dispatch_policy']['slice_ids']}
            nodes = [n for n in nodes if n.name in selected]
        # Reserve a stable window before exposing any cards to the dispatcher.
        # Failed cards do not occupy the window; their failure remains visible.
        source_window = {node.name for node in [
            item for item in nodes if item.kind == "source-prepare"
            and not domain_completed(self.workflow, value, item)
            and not self._failed_card(workflow_id, item)
        ][:self.source_prepare_concurrency]}
        pass_window = set()
        if value['batch_id']:
            capacity = self.workflow.knowledge._batch(value['batch_id'])['slice_config']['pass_worker_concurrency']
            candidates = [node for node in nodes if node.kind == 'pass-slice'
                          and self._canary_allows(value, node)
                          and not self._failed_card(workflow_id, node)
                          and self.workflow.knowledge._slice(value['batch_id'], node.name.partition(':')[2])['state']
                          in ('ready', 'leased')]
            candidates.sort(key=lambda n: self.workflow.knowledge._slice(
                value['batch_id'], n.name.partition(':')[2])['state'] != 'leased')
            pass_window = {n.name for n in candidates[:capacity]}
        slug = board_slug(workflow_id)
        self.kanban.ensure_board(slug)
        ids: dict[str, str] = {}
        task_map: list[dict[str, str]] = []
        artifact_conflicts: list[dict[str, Any]] = []
        for node in nodes:
            key = f"ingest:{workflow_id}:{node.kind}:{node.input_fingerprint}"
            pin = next((item for item in value.get("template_pins", [])
                        if item["kind"] == node.kind), None)
            slice_template_hash = (self.workflow.knowledge._slice(
                value["batch_id"], node.name.partition(":")[2])["template_hash"]
                if node.kind == "pass-slice" else None)
            instructions = (self.workflow.revision_instructions(value)
                if self.workflow.revision_allows(value, node.name) else
                (self.workflow.execution_template(value, node.name.partition(':')[2])
                if node.kind == 'pass-slice' else None) or templates.get(node.kind))
            executor = executor_for(node.kind, instructions)
            body = json.dumps({"workflow_id": workflow_id, "node": node.name,
                               "executor": executor,
                               "kind": node.kind,
                               "input_fingerprint": node.input_fingerprint,
                               "vault": str(self.workflow.vault),
                               "worker_contract": "pinned-v1" if pin else "phase-7-pending",
                               "template_id": pin["template_id"] if pin else None,
                               "template_hash": pin["template_hash"] if pin else None,
                               "slice_template_hash": slice_template_hash,
                               "dispatcher_script": str(Path(__file__).resolve().parents[2] /
                                   "hermes-obsidian-governed-ingest-orchestrator/scripts/dispatch_ingest_workflow.py"),
                               "domain_script": str(Path(__file__).resolve().parents[2] /
                                   "hermes-obsidian-controlled-ingest/scripts/manage_knowledge_build.py"),
                               "worker_request": {"workflow_id": workflow_id, "node": node.name},
                               "worker_binding": (str(_vault_path(self.workflow.vault,
                                   f"_system/ledgers/ingest-workflows/{workflow_id}/bindings/{fingerprint(key)}.json")) if pin else None),
                               "worker_command": (shlex.join(["python3", str(Path(__file__).resolve().parents[2] /
                                   "hermes-obsidian-governed-ingest-orchestrator/scripts/run_preparation_worker.py"),
                                   "--vault", str(self.workflow.vault), "--binding", str(_vault_path(self.workflow.vault,
                                   f"_system/ledgers/ingest-workflows/{workflow_id}/bindings/{fingerprint(key)}.json"))])
                                   if node.kind in ("source-prepare", "exact-plan") else None),
                               "instructions": instructions,
                               "citation_revision_rule": ("Historical superseded citation Passes are audit only. For candidate decisions use only citations whose pass_id is not named by a later supersedes_pass_id in that task; retain all Pass IDs as history. Never propose or omit superseded candidates."
                                   if value.get('pass_revision') and node.kind == 'resource-reduce' else None)},
                              ensure_ascii=False, sort_keys=True)
            # Native create ignores archived idempotency keys. Reuse the Vault's
            # current failed binding until an explicit
            # repair changes the input identity. Never mint a new card under an
            # immutable binding file merely because its predecessor is archived.
            previous = next((item for item in value['kanban']['task_map']
                             if value['kanban']['board_id'] == slug
                             and item['node'] == node.name and item['idempotency_key'] == key
                             and self._failed_card(workflow_id, node)), None)
            completed_task = self._completed_pass_binding(value, node, key)
            task_id = completed_task or (previous['task_id'] if previous else self.kanban.create_node(
                slug, node, key, [ids[parent] for parent in node.parents], body,
                enable_workers=False))  # bind the complete graph before releasing cards
            ids[node.name] = task_id
            if pin:
                record = {"workflow_id": workflow_id, "node": node.name, "task_id": task_id,
                          "actor": actor,
                          "template_hash": slice_template_hash if node.kind == 'pass-slice' else pin["template_hash"],
                          "input_fingerprint": node.input_fingerprint}
                if node.kind == 'pass-slice':
                    record['worker_id'] = 'ingest-worker-' + task_id
                path = _vault_path(self.workflow.vault,
                    f"_system/ledgers/ingest-workflows/{workflow_id}/bindings/{fingerprint(key)}.json")
                if not path.is_file():
                    if any(c.get('task_id') == task_id and c['idempotency_key'] == key
                           for c in value['kanban']['task_map']):
                        artifact_conflicts.append(record)
                    else:
                        _write_atomic(path, _json_bytes(record))
                else:
                    try:
                        matches = _load_json(path) == record
                    except (ContractError, OSError, ValueError):
                        matches = False
                    if not matches:
                        artifact_conflicts.append(record)
            task_map.append({"node": node.name, "idempotency_key": key,
                             "task_id": task_id})
        value = self.workflow.status(workflow_id)
        # A revision narrows the dispatch graph without retiring valid native
        # outcomes. Keep their identities so later projection cannot recreate
        # archived cards under an immutable binding.
        projected_names = {item['node'] for item in task_map}
        for previous in value['kanban']['task_map']:
            if previous['node'] in projected_names or not previous['node'].startswith('pass-slice:'):
                continue
            item = self.workflow.knowledge._slice(value['batch_id'], previous['node'].partition(':')[2])
            node = pass_node(value, item)
            key = f"ingest:{workflow_id}:{node.kind}:{node.input_fingerprint}"
            if previous['idempotency_key']==key and self._completed_pass_binding(value, node, key)==previous['task_id']:
                task_map.append(dict(previous))
        current_keys = {item["idempotency_key"] for item in task_map}
        if value["kanban"]["board_id"] == slug:
            for previous in value["kanban"]["task_map"]:
                replacement = next((c for c in task_map if c['idempotency_key']==previous['idempotency_key']), None)
                if replacement and replacement.get('task_id') != previous.get('task_id'):
                    self.kanban.archive(slug, previous['task_id'])
                    continue
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
                    elif (current := next((node for node in nodes
                                           if node.name == previous["node"]), None)) is not None \
                            and domain_completed(self.workflow, value, current):
                        self.kanban.complete(slug, previous["task_id"],
                                             "Authoritative Vault worker result committed")
                    else:
                        self.kanban.archive(slug, previous["task_id"])
        bind = {"workflow_id": workflow_id, "actor": actor,
                "expected_revision": value["revision"],
                "board_id": slug, "task_map": task_map}
        bind["input_digest"] = mutation_digest(bind)
        updated = self.workflow.bind_kanban(bind)
        updated = self.workflow.finish_pass_revision(self._mutation(updated))
        # A worker may commit its boundary while this graph is being projected.
        # Hold successors immediately and persist the receipt before the watcher exits.
        updated = self.workflow.latch_pause(self._mutation(updated))
        for record in artifact_conflicts:
            node = next(n for n in nodes if n.name == record['node'])
            if not self._failed_card(workflow_id, node):
                self._projection_failure(workflow_id, node, record['task_id'], 'DISPATCH_ARTIFACT_CHANGED',
                    'canonical dispatcher binding differs; trusted repair required')
        cooldown = (self.workflow.knowledge._batch(updated["batch_id"]).get("cooldown_until")
                    if updated["batch_id"] else None)
        cooldown_active = (cooldown is not None and datetime.fromisoformat(
            cooldown.replace("Z", "+00:00")) > datetime.now(timezone.utc))
        for node in nodes:
            failed_parent = next((p for p in nodes if p.name in node.parents
                                  and self._execution_blocked(workflow_id, p)), None)
            if failed_parent and not domain_completed(self.workflow, updated, node) and not self._failed_card(workflow_id, node):
                self._projection_failure(workflow_id, node, ids[node.name],
                                         "DEPENDENCY_EXECUTION_BLOCKED", failed_parent.name)
            if not domain_completed(self.workflow, updated, node) and hasattr(self.kanban, "task_snapshot"):
                snapshot = self.kanban.task_snapshot(slug, ids[node.name])
                ended = [r for r in snapshot.get("runs", []) if r.get("ended_at")]
                durable_slice_failure = False
                if ended and node.kind == 'pass-slice':
                    current_slice = self.workflow.knowledge._slice(updated['batch_id'], node.name.partition(':')[2])
                    error = current_slice.get('last_error')
                    if error and current_slice['state'] != 'leased':
                        try:
                            failure_at = datetime.fromisoformat(error['at'].replace('Z', '+00:00')).timestamp()
                            durable_slice_failure = failure_at >= float(ended[-1]['started_at'])
                        except (KeyError, TypeError, ValueError):
                            pass
                if ended and not durable_slice_failure and snapshot['task']['status'] not in ('running', 'archived') and not self._failed_card(workflow_id, node):
                    self._projection_failure(workflow_id, node, ids[node.name], "NATIVE_WORKER_STOPPED",
                        str(ended[-1].get("error") or ended[-1].get("summary") or "worker ended without a Vault outcome"))
            if domain_completed(self.workflow, updated, node):
                summary = "Authoritative Vault outcome already committed"
                if node.kind == 'source-prepare':
                    for item in updated.get('source_outcomes', []):
                        name = 'source-prepare:' + fingerprint({'path':item['path'],
                            'content_sha256':item['content_sha256']})[:16]
                        if name == node.name:
                            summary = ('Source preparation ready' if item['status']=='ready' else
                                f"Source preparation failed ({item['error_code']}): {item['reason']}; coverage gap retained")
                            break
                self.kanban.complete(slug, ids[node.name],
                                     summary)
            elif self._failed_card(workflow_id, node):
                if not self._execution_blocked(workflow_id, node):
                    self.kanban.wait(slug, ids[node.name], 'blocked', 'Vault validation gate requires review')
                    continue
                # Publish dependency guards before archiving can promote children.
                affected = {node.name}
                for dependent in nodes:
                    if any(parent in affected for parent in dependent.parents):
                        affected.add(dependent.name)
                        if not self._failed_card(workflow_id, dependent):
                            self._projection_failure(workflow_id, dependent, ids[dependent.name],
                                                     'DEPENDENCY_EXECUTION_BLOCKED', node.name)
                for dependent in reversed(nodes):
                    if dependent.name in affected and dependent.name != node.name:
                        self.kanban.archive(slug, ids[dependent.name])
                self.kanban.archive(slug, ids[node.name])
            elif node.gate or not self._eligible(updated, node, nodes):
                self.kanban.wait(slug, ids[node.name], "blocked",
                                 "Vault gate or outside active dispatch scope")
            elif node.kind == "source-prepare" and node.name not in source_window:
                self.kanban.wait(slug, ids[node.name], "blocked",
                                 "Skill source preparation concurrency limit")
            elif node.kind == "pass-slice":
                slice_id = node.name.partition(":")[2]
                state = self.workflow.knowledge._slice(updated["batch_id"], slice_id)["state"]
                if state == "retry_wait" or (state == "ready" and cooldown_active):
                    self.kanban.wait(slug, ids[node.name], "scheduled", "Vault retry backoff")
                elif state in ("blocked", "reconcile_required", "awaiting_approval",
                               "cancelled"):
                    self.kanban.wait(slug, ids[node.name], "blocked", "Vault worker gate")
                elif state == "ready":
                    if node.name in pass_window:
                        self.kanban.unblock(slug, ids[node.name])
                    else:
                        self.kanban.wait(slug, ids[node.name], 'blocked', 'Vault Pass concurrency limit')
            else:
                self.kanban.unblock(slug, ids[node.name])
        active_canary = self.enable_workers and any(
            not domain_completed(self.workflow, updated, node)
            and not self._failed_card(workflow_id, node)
            and self._eligible(updated, node, nodes)
            and (node.kind != "pass-slice" or self.workflow.knowledge._slice(
                updated["batch_id"], node.name.partition(":")[2])["state"]
                in ("ready", "leased")) for node in nodes)
        canary_done = (updated["dispatch_policy"]["mode"] == "canary"
                       and len(updated["dispatch_policy"]["slice_ids"]) == 8
                       and all(self.workflow.knowledge._slice(updated["batch_id"], sid)["state"] == "completed"
                               for sid in updated["dispatch_policy"]["slice_ids"]))
        canary_report = None
        if canary_done:
            canary_report = self._report(updated, "canary", {
                "workflow_id": workflow_id, "ok": True,
                "slice_ids": updated["dispatch_policy"]["slice_ids"],
                "selection_digest": updated["dispatch_policy"]["selection_digest"],
                "source_coverage": self.workflow.source_coverage(updated),
                "execution_mode": updated["scope"].get("execution_mode", "manual")})
        return {"workflow_id": workflow_id, "workflow_created": True,
                "state": "paused" if self.workflow.pending_pause(updated) else updated["state"],
                "pause": self.workflow.pause_status(updated),
                "revision": updated["revision"], "background_dispatch": active_canary,
                "program_dispatch": any(executor_for(n.kind, self.workflow.revision_instructions(updated)
                    if self.workflow.revision_allows(updated, n.name) else
                    (self.workflow.execution_template(updated, n.name.partition(':')[2])
                    if n.kind == 'pass-slice' else None) or templates.get(n.kind)) != 'model'
                    and not domain_completed(self.workflow, updated, n)
                    and not self._failed_card(workflow_id, n)
                    and self._eligible(updated, n, nodes) for n in nodes),
                "board_id": slug, "task_count": len(task_map),
                "canary_complete": canary_done, "canary_report_ref": canary_report,
                "execution_blocked": [n.name for n in nodes if self._execution_blocked(workflow_id, n)],
                "pass_revision": ({'revision_id': updated['pass_revision']['revision_id'],
                                   'state': updated['pass_revision']['state']}
                                  if updated.get('pass_revision') else None)}

    def _slice_worker(self, workflow_id: str, node_name: str,
                      task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.enable_workers:
            _fail("WORKER_CONTRACT_UNAVAILABLE", "fixed worker templates are not installed")
        workflow = self.workflow.status(workflow_id)
        self.workflow.assert_execution_ready(workflow)
        if not self.workflow.revision_allows(workflow, node_name):
            self.workflow.assert_not_paused(workflow)
        self.workflow.pinned_templates(workflow)
        policy = workflow.get("dispatch_policy", {})
        if (policy.get("mode") not in ("canary", "full") or not node_name.startswith("pass-slice:")
                or (policy.get("mode") == "canary" and
                    node_name.partition(":")[2] not in policy.get("slice_ids", []))):
            _fail("ACCESS_DENIED", "Pass slice is outside the armed canary")
        if workflow["cancel_requested"] or workflow["current_stage"] != "analyzing":
            _fail("WORKFLOW_STOPPED", "Pass work is outside the active analyzing stage")
        matching = [item for item in workflow["kanban"]["task_map"]
                    if item["node"] == node_name and item.get("task_id") == task_id]
        if len(matching) != 1 or not node_name.startswith("pass-slice:"):
            _fail("ACCESS_DENIED", "Kanban task is not bound to this Pass slice")
        slice_id = node_name.partition(":")[2]
        value = self.workflow.knowledge._slice(workflow["batch_id"], slice_id)
        node = pass_node(workflow, value)
        if matching[0]["idempotency_key"] != (
                f"ingest:{workflow_id}:{node.kind}:{node.input_fingerprint}"):
            _fail("STALE_INPUT", "Kanban node fingerprint changed")
        if self._execution_blocked(workflow_id, node):
            _fail('EXECUTION_BLOCKED', 'slice binding requires trusted repair')
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
        from hermes_source_units.model_projection import RENDERER
        if value['template_id'] == RENDERER:
            plan, group = self.workflow.execution_slice(workflow, value['slice_id'])
            if [i['task_id'] for i in group] != value['task_ids']:
                _fail('STALE_INPUT', 'compact slice task ownership changed')
            for expected, measured in zip(group, inputs):
                projection = self.workflow.knowledge.preview_model_input(expected['task_id'], batch)
                if (measured['fingerprint'] != expected['canonical_fingerprint'] or
                        projection['input_id'] != expected['input_id']):
                    _fail('STALE_INPUT', 'canonical or model input changed after execution planning')
            observed = 'sha256:' + fingerprint({'amendment_id':plan['amendment_id'], 'tasks':group,
                'config':plan['config'], 'renderer':RENDERER,'template_hash':value['template_hash']})
            if observed != value['input_fingerprint']:
                _fail('STALE_INPUT', 'compact execution fingerprint changed')
        elif not value.get("reslice_count"):
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
        self.validate_worker_request(request, beginning=beginning)
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
        if request.get('input_fingerprint', node.input_fingerprint) != node.input_fingerprint:
            _fail('STALE_INPUT', 'dispatcher input fingerprint changed')
        if self._execution_blocked(workflow['workflow_id'], node):
            _fail("EXECUTION_BLOCKED", "current binding requires trusted repair before retry")
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

    def _batch_worker(self, request: Mapping[str, Any], *, beginning: bool = False,
                      completing: bool = False) -> tuple[dict[str, Any], Node]:
        if not self.enable_workers:
            _fail("WORKER_CONTRACT_UNAVAILABLE", "fixed worker templates are not installed")
        workflow = self.workflow.status(str(request["workflow_id"]))
        if not completing:
            self.workflow.assert_not_paused(workflow)
        self.workflow.pinned_templates(workflow)
        if (workflow["cancel_requested"] or workflow["batch_id"] is None
                or workflow["scope"].get("execution_mode") != "auto_full"
                or workflow["dispatch_policy"]["mode"] != "full"):
            _fail("WORKFLOW_STOPPED", "automatic batch work is not active")
        nodes = desired_graph(self.workflow, workflow)
        node = next((item for item in nodes if item.name == request["node"]), None)
        if node is None or node.kind in ("source-prepare", "exact-plan", "pass-slice") or node.gate:
            _fail("ACCESS_DENIED", "card is not a dispatchable batch worker")
        matching = [item for item in workflow["kanban"]["task_map"]
                    if item["node"] == node.name and item.get("task_id") == request["task_id"]]
        key = f"ingest:{workflow['workflow_id']}:{node.kind}:{node.input_fingerprint}"
        if len(matching) != 1 or matching[0]["idempotency_key"] != key:
            _fail("STALE_INPUT", "batch worker card binding changed")
        if self._execution_blocked(workflow['workflow_id'], node):
            _fail('EXECUTION_BLOCKED', 'batch worker binding requires trusted repair')
        by_name = {item.name: item for item in nodes}
        if not (self._canary_allows(workflow, node, completing=completing)
                and all(domain_completed(self.workflow, workflow, by_name[parent]) for parent in node.parents)):
            _fail("INCOMPLETE_COVERAGE", "batch worker stage or parent outcome is not ready")
        pin = next(item for item in workflow["template_pins"] if item["kind"] == node.kind)
        if not beginning and request.get("template_hash") != pin["template_hash"]:
            _fail("STALE_INPUT", "batch worker template hash changed")
        return workflow, node

    def _report(self, workflow: Mapping[str, Any], name: str,
                payload: Mapping[str, Any]) -> str:
        ref = (f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/"
               f"reports/{name}.json")
        _write_atomic(_vault_path(self.workflow.vault, ref), _json_bytes(payload))
        return ref

    def _lint(self) -> dict[str, Any]:
        script = (Path(__file__).resolve().parents[2] /
                  "hermes-obsidian-vault-lint/scripts/lint_vault.py")
        if not script.is_file():
            _fail("VALIDATOR_UNAVAILABLE", "Vault lint Skill is not installed")
        result = subprocess.run([sys.executable, str(script), "--vault",
                                 str(self.workflow.vault), "--profile", "post-ingest",
                                 "--json"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300,
                                check=False)
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            _fail("VALIDATOR_UNAVAILABLE", result.stderr.strip() or "Vault lint returned no JSON")
        if result.returncode not in (0, 1, 2):
            _fail("VALIDATOR_UNAVAILABLE", result.stderr.strip() or "Vault lint failed")
        return payload

    def _approve_automatic(self, value: Mapping[str, Any], checkpoint: str,
                           evidence: Mapping[str, Any], lint: Mapping[str, Any] | None = None) -> dict[str, Any]:
        digest = value["checkpoints"][checkpoint]["approval_digest"]
        decision = {"contract": "hermes-ingest-checkpoint-decision/v1",
                    "workflow_id": value["workflow_id"], "checkpoint": checkpoint,
                    "approval_digest": digest, "ok": True, "blocking_codes": [],
                    "evidence": dict(evidence)}
        if lint is not None:
            decision["lint"] = dict(lint)
        ref = (f"_system/ledgers/ingest-workflows/{value['workflow_id']}/"
               f"decisions/{checkpoint}.json")
        path = _vault_path(self.workflow.vault, ref)
        encoded = _json_bytes(decision)
        if path.exists() and path.read_bytes() != encoded:
            _fail("IDEMPOTENCY_CONFLICT", "checkpoint decision report already differs")
        if not path.exists():
            _write_atomic(path, encoded)
        return self.workflow.approve(self._mutation(
            value, checkpoint=checkpoint, approval_digest=digest,
            decision_mode="auto_verified", decision_ref=ref))

    def _complete_batch_worker(self, workflow: dict[str, Any], node: Node,
                               request: Mapping[str, Any]) -> dict[str, Any]:
        kind = node.kind
        batch_id = workflow["batch_id"]
        updated = workflow
        if kind in ("resource-reduce", "global-reduce"):
            if not domain_completed(self.workflow, workflow, node):
                _fail("INCOMPLETE_COVERAGE", f"{kind} has no durable Vault result")
        elif kind == "checkpoint-1-validate":
            validation = self.workflow.knowledge.validate_batch(batch_id)
            report = self._report(workflow, "checkpoint-1-validation", validation)
            if not validation["ok"]:
                self._report(workflow, f"failed-{fingerprint(node.name)[:16]}",
                             {"node": node.name, "input_fingerprint": node.input_fingerprint,
                              "report_ref": report, "blocking_codes": [
                                  item.get("code", "VALIDATION_FAILED")
                                  for item in validation["failures"]]})
                return self._pending_reconciliation({"ok": False, "report_ref": report,
                        "blocking_codes": [item.get("code", "VALIDATION_FAILED")
                                           for item in validation["failures"]]})
            updated = self._advance(workflow, "checkpoint_1", [report])
            updated = self._approve_automatic(updated, "checkpoint_1", validation)
        elif kind == "build-finalize":
            batch = self.workflow.knowledge._batch(batch_id)
            if batch["state"] != "completed" or not domain_completed(self.workflow, workflow, node):
                _fail("INCOMPLETE_COVERAGE", "all build runs must be completed")
        elif kind == "vault-finalize-plan":
            updated = self.workflow.bind_release_plan(self._mutation(
                workflow, release_id=str(request["release_id"]),
                plan_id=str(request["plan_id"])))
        elif kind == "checkpoint-2-validate":
            if not workflow.get("release_id"):
                _fail("INCOMPLETE_COVERAGE", "release plan is not bound")
            lint = self._lint()
            report = self._report(workflow, "checkpoint-2-lint", lint)
            if not lint.get("ok"):
                self._report(workflow, f"failed-{fingerprint(node.name)[:16]}",
                             {"node": node.name, "input_fingerprint": node.input_fingerprint,
                              "report_ref": report, "blocking_codes": [
                                  item.get("code", "LINT_FAILED") for item in lint.get("issues", [])
                                  if item.get("severity") == "error"]})
                return self._pending_reconciliation({"ok": False, "report_ref": report,
                        "blocking_codes": [item.get("code", "LINT_FAILED")
                                           for item in lint.get("issues", [])
                                           if item.get("severity") == "error"]})
            plan_ref = f"_system/knowledge-releases/{workflow['release_id']}/plan.json"
            updated = self._advance(workflow, "checkpoint_2", [plan_ref, report])
            updated = self._approve_automatic(updated, "checkpoint_2",
                                              {"plan_ref": plan_ref, "lint_ref": report}, lint)
        elif kind == "release-apply":
            release_id = workflow.get("release_id")
            if not release_id:
                _fail("INCOMPLETE_COVERAGE", "release plan is not bound")
            checked = FileVaultFinalizeService(self.workflow.vault).validate(release_id)
            if not checked["ok"]:
                _fail("INCOMPLETE_COVERAGE", "release validation failed")
            manifest_ref = f"_system/knowledge-releases/{release_id}/manifest.json"
            updated = self._advance(workflow, "indexing", [manifest_ref])
        elif kind == "provider-sync":
            ref = "_system/reports/retrieval-index-manifest.json"
            manifest = _load_json(_vault_path(self.workflow.vault, ref))
            release_ref = f"_system/knowledge-releases/{workflow['release_id']}/manifest.json"
            release_hash = hashlib.sha256(_vault_path(self.workflow.vault, release_ref).read_bytes()).hexdigest()
            if (manifest.get("status") != "ready" or
                    manifest.get("release_id") != workflow["release_id"] or
                    manifest.get("release_hash") != release_hash):
                _fail("INCOMPLETE_COVERAGE", "Provider index is not ready for the exact release")
            updated = self._advance(workflow, "validating", [ref])
        elif kind == "acceptance":
            release_id = workflow.get("release_id")
            checked = FileVaultFinalizeService(self.workflow.vault).validate(release_id)
            lint = self._lint()
            report = self._report(workflow, "acceptance-lint", lint)
            if not checked["ok"] or not lint.get("ok"):
                self._report(workflow, f"failed-{fingerprint(node.name)[:16]}",
                             {"node": node.name, "input_fingerprint": node.input_fingerprint,
                              "report_ref": report, "blocking_codes": [
                                  item.get("code", "LINT_FAILED") for item in lint.get("issues", [])
                                  if item.get("severity") == "error"]})
                return self._pending_reconciliation({"ok": False, "report_ref": report,
                        "blocking_codes": [item.get("code", "LINT_FAILED")
                                           for item in lint.get("issues", [])
                                           if item.get("severity") == "error"]})
            release_ref = f"_system/knowledge-releases/{release_id}/manifest.json"
            target = ("partial" if self.workflow.source_coverage(workflow)["failed"]
                      else "completed")
            updated = self._advance(workflow, target, [release_ref, report])
        else:
            _fail("ACCESS_DENIED", "unsupported automatic batch worker")
        latest = self.workflow.status(workflow["workflow_id"])
        result = {"ok": True, "node": node.name, "workflow_state": latest["state"]}
        return self._pending_reconciliation(result)

    def worker_register_source(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Assign technical identities without asserting origin authority or approving it."""
        scripts = Path(__file__).resolve().parents[2] / "hermes-obsidian-controlled-ingest/scripts"
        sys.path.insert(0, str(scripts))
        from governance_repository import GovernanceError
        try:
            return self._register_source(request)
        except GovernanceError as exc:
            _fail("SOURCE_REGISTRATION_BLOCKED", str(exc))

    def _register_source(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workflow, node, source = self._pre_worker(request)
        if node.kind != "source-prepare":
            _fail("ACCESS_DENIED", "registration is restricted to a source card")
        from governance_repository import JsonGovernanceRepository, utc_now
        repository = JsonGovernanceRepository(self.workflow.vault)
        with workflow_write_guard(self.workflow.vault, actor=workflow["actor"],
                                  workflow_id=workflow["workflow_id"], task_id=request["task_id"]):
            organizations, registry = repository.load_state()
            matches = [item for item in registry["records"] if item["content_sha256"] == source["content_sha256"]]
            if len(matches) > 1:
                _fail("IDEMPOTENCY_CONFLICT", "multiple existing identities for source bytes")
            if matches:
                record = matches[0]
                if record["storage_uri"] != "local://" + source["path"]:
                    _fail("SOURCE_IDENTITY_CONFLICT", "same-content source has a different registered path; reconcile provenance explicitly")
                return {"ok": True, "reused": True, "source": source,
                        "document_id": record["document_id"], "version_id": record["version_id"],
                        "resource_id": record["resource_id"], "registry_revision": registry["registry_revision"]}
            # Unknown origin is represented explicitly; the ID is an internal grouping key.
            vault_key = fingerprint(repository.manifest["vault"]["id"])[:20]
            org_id = "organization-unresolved-" + vault_key
            if not any(item["id"] == org_id for item in organizations["organizations"]):
                repository.add_organization(organization_id=org_id,
                    name="Unresolved source organization (technical intake group)", aliases=[],
                    status="candidate", expected_revision=organizations["registry_revision"], actor=workflow["actor"])
            sha = source["content_sha256"]
            now = utc_now()
            collection = "collection-intake-" + vault_key
            record = {"document_id": "doc-" + sha, "version_id": "version-" + sha,
                      "resource_id": "resource-" + sha, "collection_id": collection,
                      "title": Path(source["path"]).stem, "business_version": None,
                      "storage_uri": "local://" + source["path"], "content_sha256": sha,
                      "processing_status": "processing", "governance_status": "candidate",
                      "authority_status": "unknown", "supersedes_version_id": None,
                      "created_at": now, "updated_at": now,
                      "source_occurrences": [{"source_occurrence_id": "occurrence-" + fingerprint(source),
                          "source_organization_id": org_id, "source_collection_id": collection,
                          "batch_id": None, "original_relative_path": source["path"], "received_at": now}]}
            result = repository.register(record, registry["registry_revision"], workflow["actor"])
            return {"ok": True, "reused": False, "source": source,
                    "document_id": record["document_id"], "version_id": record["version_id"],
                    "resource_id": record["resource_id"], "registry_revision": result["registry_revision"]}

    def worker_begin(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_worker_request(request, beginning=True)
        if str(request["node"]).startswith("source-prepare:") or request["node"] == "exact-plan":
            workflow, node, source = self._pre_worker(request, beginning=True)
            if node.kind == "source-prepare" and domain_completed(self.workflow, workflow, node):
                _fail("INVALID_TRANSITION", "source outcome is already recorded")
            pin = next(item for item in workflow["template_pins"] if item["kind"] == node.kind)
            return {"ok": True, "node": node.name, "template_hash": pin["template_hash"],
                    "input_fingerprint": node.input_fingerprint,
                    "source": source, "assigned_source": source,
                    "scope_note": "assigned_source is the ONLY source this card may process; source_coverage is read-only whole-workflow progress, not assigned work",
                    "actor": workflow["actor"],
                    "source_coverage": self.workflow.source_coverage(workflow)}
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node = self._batch_worker(request, beginning=True)
            pin = next(item for item in workflow["template_pins"] if item["kind"] == node.kind)
            return {"ok": True, "node": node.name, "batch_id": workflow["batch_id"],
                    "release_id": workflow.get("release_id"),
                    "template_hash": pin["template_hash"],
                    "input_fingerprint": node.input_fingerprint}
        workflow_id, node_name = str(request["workflow_id"]), str(request["node"])
        workflow, value = self._slice_worker(workflow_id, node_name,
                                             str(request["task_id"]))
        worker_id = str(request["worker_id"])
        with worker_binding(dict(request)), workflow_write_guard(
                self.workflow.vault, kinds=('pass-slice',), actor=workflow['actor']):
            result = self.workflow.knowledge.batch_next_slice(
                workflow["batch_id"], worker_id, slice_id=value["slice_id"],
                lock_timeout=WORKER_LOCK_TIMEOUT,
                lock_check=lambda: self._slice_worker(workflow_id, node_name, str(request['task_id'])))
            if not result["leased"]:
                return result
            bound = {**request, 'expected_revision':result['slice']['revision'],
                     'template_hash':result['slice']['template_hash'], 'actor':workflow['actor']}
            prepared = self.workflow.knowledge.prepare_leased_slice(
                workflow['batch_id'], value['slice_id'], worker_id, result['slice']['revision'],
                check=lambda: self.worker_check(bound), timeout=WORKER_LOCK_TIMEOUT)
            effective = (self.workflow.execution_template(workflow, value['slice_id'])
                         or self.workflow.pinned_templates(workflow)['pass-slice'])
            if ((self.workflow.execution_template(workflow, value['slice_id']) is not None
                    and not self.workflow.revision_allows(workflow, node_name))
                    or semantic_template('pass-slice', effective)):
                from hermes_source_units.model_projection import RENDERER
                if result['slice']['template_id'] == RENDERER or semantic_template('pass-slice', effective):
                    projections = []
                    for descriptor in prepared['reading_packages']:
                        package = _load_json(_vault_path(self.workflow.vault, descriptor['path']))
                        projections.append(self.workflow.knowledge.model_input(descriptor['task_id'], package))
                    packet = self.workflow.knowledge.model_packet(projections, workflow['batch_id'], persist=True)
                    # Canonical packages and task snapshots never reach the model.
                    receipt = {'ok':True, 'leased':True, 'model_input':{'path':packet['path'],
                        'input_codepoints':packet['codepoints'],'task_count':len(projections)},
                               'input_codepoints':0, 'worker_request':bound}
                    # Count the actual UTF-8 JSON presentation (codepoints), not
                    # an unformatted proxy. The renderer writes compact view files.
                    receipt['input_codepoints'] = packet['codepoints']
                    while True:
                        measured = packet['codepoints'] + len(json.dumps(receipt, ensure_ascii=False, indent=2)) + 1
                        if measured == receipt['input_codepoints']:
                            break
                        receipt['input_codepoints'] = measured
                    config = self.workflow.knowledge._batch(workflow['batch_id'])['slice_config']
                    if receipt['input_codepoints'] > config['slice_max_input_codepoints']:
                        _fail('READING_WINDOW_OVERSIZE', 'actual compact worker input exceeds budget')
                    return receipt
        return {"ok": True, "leased": True, "slice": result["slice"],
                "template_hash": result["slice"]["template_hash"],
                "task_ids": result["slice"]["task_ids"], **prepared,
                "worker_request": bound,
                **({'pass_revision': {'revision_id': workflow['pass_revision']['revision_id'],
                    'tasks': [t for t in workflow['pass_revision']['tasks'] if t['task_id'] in value['task_ids']]}}
                    if self.workflow.revision_allows(workflow, node_name) else {})}

    def worker_check(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_worker_request(request)
        if str(request["node"]).startswith("source-prepare:") or request["node"] == "exact-plan":
            workflow, node, source = self._pre_worker(request)
            return {"ok": True, "node": node.name, "source": source,
                    "source_coverage": self.workflow.source_coverage(workflow)}
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node = self._batch_worker(request)
            return {"ok": True, "node": node.name, "batch_id": workflow["batch_id"],
                    "release_id": workflow.get("release_id"),
                    "input_fingerprint": node.input_fingerprint}
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
                "slice_id": value["slice_id"], "task_ids": ([t['task_id'] for t in workflow['pass_revision']['tasks']
                    if t['task_id'] in value['task_ids']] if self.workflow.revision_allows(workflow, request['node']) else value["task_ids"]),
                "input_fingerprint": value["input_fingerprint"],
                "worker_request": dict(request)}

    def worker_heartbeat(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not str(request["node"]).startswith("pass-slice:"):
            return self.worker_check(request)
        checked = self.worker_check(request)
        with worker_binding(dict(request)), workflow_write_guard(
                self.workflow.vault, kinds=('pass-slice',)):
            result = self.workflow.knowledge.slice_heartbeat(
                checked["batch_id"], checked["slice_id"],
                str(request["worker_id"]), int(request["expected_revision"]),
                lock_timeout=WORKER_LOCK_TIMEOUT, lock_check=lambda: self.worker_check(request))
        result["worker_request"] = {**request,
                                    "expected_revision": result["slice"]["revision"]}
        return result

    def worker_complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if str(request["node"]).startswith("source-prepare:") or request["node"] == "exact-plan":
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
            return self._pending_reconciliation(result)
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node = self._batch_worker(request, completing=True)
            return self._complete_batch_worker(workflow, node, request)
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
            if self.workflow.revision_allows(workflow, node_name):
                target = next((t for t in workflow['pass_revision']['tasks'] if t['task_id'] == task_id), None)
                if target and not any(record.get('revision_id') == workflow['pass_revision']['revision_id']
                        and record.get('supersedes_pass_id') == target['pass_id'] for _, record in records):
                    _fail('INCOMPLETE_COVERAGE', 'selected task needs its authorized revision citation')
            if (0 not in sequences or not any(sequence > 0 for sequence in sequences)
                    or sequences != set(range(max(sequences) + 1))):
                _fail("INCOMPLETE_COVERAGE", "each slice task needs candidate and citation Passes")
            result_refs.extend(path.relative_to(self.workflow.vault).as_posix()
                               for path, _ in records)
        if value["state"] == "completed":
            if sorted(value["result_refs"]) != sorted(result_refs):
                _fail("IDEMPOTENCY_CONFLICT", "completed slice Pass set changed")
        else:
            with worker_binding(dict(request)), workflow_write_guard(
                    self.workflow.vault, kinds=('pass-slice',)):
                self.worker_check(request)
                self.workflow.knowledge.slice_complete({
                    "batch_id": workflow["batch_id"], "slice_id": value["slice_id"],
                    "worker_id": request["worker_id"],
                    "expected_revision": request["expected_revision"],
                    "result_refs": result_refs,
                }, lock_timeout=WORKER_LOCK_TIMEOUT, lock_check=lambda: self.worker_check(request))
        return self._pending_reconciliation({"ok": True, "slice_id": value["slice_id"],
                "result_refs": result_refs})

    def worker_fail(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if str(request["node"]).startswith("source-prepare:") or request["node"] == "exact-plan":
            workflow, node, source = self._pre_worker(request)
            if node.kind == "exact-plan" or request["code"] not in SOURCE_FAILURE_CODES:
                report = self._report(workflow, f"failed-{fingerprint(node.name)[:16]}", {
                    "node": node.name, "input_fingerprint": node.input_fingerprint,
                    "error_code": str(request["code"]), "reason": str(request["message"]),
                    "source_failed": False})
                return self._pending_reconciliation({"ok": False, "error_code": request["code"],
                        "report_ref": report,
                        "source_coverage": self.workflow.source_coverage(workflow)})
            outcome = {"workflow_id": workflow["workflow_id"], "actor": workflow["actor"],
                       "expected_revision": workflow["revision"], **source,
                       "status": "failed", "error_code": request["code"],
                       "reason": request["message"],
                       "artifact_refs": request.get("artifact_refs", [])}
            outcome["input_digest"] = mutation_digest(outcome)
            updated = self.workflow.record_source_outcome(outcome)
            return self._pending_reconciliation({"ok": True,
                    "source_coverage": self.workflow.source_coverage(updated)})
        if not str(request["node"]).startswith("pass-slice:"):
            workflow, node = self._batch_worker(request)
            report = self._report(workflow, f"failed-{fingerprint(node.name)[:16]}",
                                  {"node": node.name, "input_fingerprint": node.input_fingerprint,
                                   "error_code": str(request["code"]),
                                   "reason": str(request["message"])})
            return self._pending_reconciliation({"ok": False, "node": node.name,
                    "report_ref": report, "error_code": request["code"]})
        checked = self.worker_check(request)
        outcome = self.workflow.knowledge.slice_fail({
            "batch_id": checked["batch_id"], "slice_id": checked["slice_id"],
            "worker_id": request["worker_id"],
            "expected_revision": request["expected_revision"],
            "code": request["code"], "message": request["message"],
            **({"failed_output": request["failed_output"]}
               if "failed_output" in request else {}),
        })
        return self._pending_reconciliation(outcome)
