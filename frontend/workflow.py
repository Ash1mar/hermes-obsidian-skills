"""Submit the existing independent short workflow and project its durable evidence."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4


ORCHESTRATOR = "hermes-obsidian-governed-ingest-orchestrator"
BOOTSTRAP = "hermes-obsidian-vault-bootstrap"
FINALIZE = "hermes-obsidian-knowledge-finalize"
STAGES = (
    ("bootstrap", "创建 Vault", "Bootstrap · 登记并复制授权原件"),
    ("prepare", "来源准备", "MinerU · 转换 PDF 和校验 Bundle"),
    ("exact", "精确计划与交接", "Exact-plan · 固定来源边界与资格"),
    ("topics", "主题提取", "Pass · 从来源提取初始主题"),
    ("mapping", "证据映射", "Map · 将主题关联到原文和资产"),
    ("coordination", "页面协调", "Reduce · 确定页面范围和边界"),
    ("writing", "分段写作", "Reduce · 写作、合并与覆盖检查"),
    ("formal-build", "正式构建", "Finalize · 原生 Build 与版本提交"),
    ("release", "发布与索引", "Release · 应用发布并同步 Provider"),
    ("acceptance", "最终验收", "Acceptance · 核对发布、索引与原生任务"),
)
STAGE_ORDER = {key: index for index, (key, _, _) in enumerate(STAGES)}
STAGE_FILE = re.compile(r"stage-(\d+)-(.+)\.json")
STAGE_KEYS = {"topic-seed": "topics", "map": "mapping", "local-map": "mapping",
              "coordination": "coordination", "coordination_held": "coordination",
              "local_coordination": "coordination", "local_page_detail": "coordination",
              "writing": "writing", "offline_build": "formal-build"}
ACTION_NODES = {"start": "bootstrap", "attach-source": "prepare", "mapping": "mapping",
                "coordination": "coordination", "writing-held": "writing",
                "writing-replay": "writing", "formal-replay": "formal-build", "delivery": "formal-build"}
SOURCE_TERMINAL = {"failed", "cancelled", "completed", "partial"}


class UserError(ValueError):
    pass


def read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}


def save(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def local_path(value, distro):
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 4096:
        raise UserError("请输入完整的本机路径。")
    value = value.strip()
    drive = re.fullmatch(r"([A-Za-z]):[\\/](.*)", value)
    unc = re.fullmatch(r"\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)\\(.*)", value, re.I)
    if drive:
        value = f"/mnt/{drive[1].lower()}/" + drive[2].replace("\\", "/")
    elif unc:
        if unc[1].casefold() != distro.casefold():
            raise UserError(f"请使用当前 WSL 发行版 {distro} 的路径。")
        value = "/" + unc[2].replace("\\", "/")
    if not value.startswith("/") or value.startswith("//"):
        raise UserError("请使用 WSL 绝对路径、Windows 盘符路径或当前 WSL 的共享路径。")
    return Path(value)


def new_request(body, config):
    sources_value = body.get("sources")
    if not isinstance(sources_value, list) or not sources_value:
        raise UserError("请输入至少一个 PDF 或目录。")
    name = body.get("vault_name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise UserError("请输入 1 至 120 字的 Vault 名称。")
    target = local_path(body.get("vault_path"), config["distro"])
    if target.is_symlink() or target != target.resolve():
        raise UserError("请填写 Vault 的实际路径。")
    state = Path(config["state"])
    if target.is_relative_to(state) or state.is_relative_to(target):
        raise UserError("Vault 地址必须与控制台运行记录目录分开。")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise UserError("请为新测试使用空目录或尚未创建的 Vault 路径。")
    files = set()
    for item in sources_value:
        source = local_path(item, config["distro"]).resolve()
        if source.is_dir() and target.is_relative_to(source):
            raise UserError("请把目标 Vault 放在输入目录之外。")
        if source.is_dir():
            found = [p.resolve() for p in source.rglob("*")
                     if p.suffix.lower() == ".pdf" and not p.is_symlink() and p.is_file()]
            if not found:
                raise UserError(f"目录中没有 PDF：{source}")
            files.update(found)
        elif source.is_file() and source.suffix.lower() == ".pdf":
            files.add(source)
        else:
            raise UserError(f"找不到 PDF 或目录：{source}")
    if any(path.is_relative_to(target) for path in files):
        raise UserError("原件必须位于目标 Vault 之外。")
    duplicates = [n for n, count in Counter(p.name.casefold() for p in files).items() if count > 1]
    if duplicates:
        raise UserError("原件将按文件名导入，以下文件名重复：" + "、".join(sorted(duplicates)))
    run = "console-" + uuid4().hex[:20]
    return {
        "operation_id": run, "actor": "user-operator", "sources": sorted(map(str, files)),
        "vault_id": run, "vault_name": name.strip(), "vault_path": str(target),
        "security_domain": "internal-test", "test_mode": "bootstrap-short-test/v1",
        "pause_after": ["exact_plan"], "canary_policy": "sample",
        "retrieval_policy": {"contract": "vault-retrieval-policy/v1",
                             "sync_enabled": True, "query_enabled": False},
    }


def ledger_path(request):
    return (Path(request["vault_path"]) / "_system/ledgers/ingest-workflows"
            / f"ingest-{request['operation_id']}.json")


def attempts_for(directory, historical=False):
    if not historical:
        return [entry for p in sorted((directory / "attempts").glob("*/attempt.json")) if (entry := read(p))]
    entries = []
    for path in directory.glob("*exit.json"):
        match = re.fullmatch(r"(exit|recovery-(?:\d+-)?exit)\.json", path.name)
        if not match:
            continue
        name = match[1]
        stem = name.removesuffix("exit")
        result = read(path)
        if "returncode" in result:
            metadata = read(directory / (f"{stem}start.json" if stem else "pipeline.json"))
            entry = metadata.get("entry")
            entries.append({"id": "history" if not stem else "history-" + stem.rstrip("-"),
                            "action": "start" if not stem else "coordination" if entry else "delivery",
                            "label": "原始运行" if not stem else f"恢复 · {entry}" if entry else "恢复交付",
                            "log": f"{stem.rstrip('-') or 'pipeline'}.log", "entry": entry,
                            "started_at": metadata.get("started_at"),
                            "returncode": result["returncode"], "ended_at": result.get("exited_at")})
    return sorted(entries, key=lambda row: row["ended_at"] or 0)


def available_actions(root, request, attempts, live, historical, source, attachment_node, operator, pause_request):
    if historical or not attempts:
        return {}
    last = attempts[-1]
    if live:
        if (last.get("operator") and operator.get("state") in (None, "running")
                and not pause_request):
            fallback = {"attach-source": "prepare", "continue-operation": last.get("node")}.get(last.get("action"))
            current_index = STAGE_ORDER.get(operator.get("node", fallback), -1)
            return {key: [{"id": "pause", "label": "在此节点前暂停", "gateway_required": False}]
                    for key in STAGE_ORDER if STAGE_ORDER[key] > current_index}
        return {}
    if operator.get("state") == "paused":
        node = operator["node"]
        return {node: [{"id": "continue-operation", "label": "继续并复用已有成果",
                        "pause_targets": list(STAGE_ORDER)[STAGE_ORDER[node] + 1:]}]}
    native = root / "native"
    original = read(root / "final-acceptance.json")
    short = read(native / "result.json")
    vault = Path(request["vault_path"])
    release = read(vault / "_system/metadata/knowledge-release-state.json").get("current_release_id")
    accepted = [("initial", original)] if original.get("final_acceptance") else []
    for attempt in attempts:
        if attempt.get("action") not in {"mapping", "coordination", "writing-held", "writing-replay"}:
            continue
        result = read(native / "reentries" / attempt["id"] / "final-acceptance.json")
        if result.get("final_acceptance"):
            accepted.append((attempt["id"], result))
    base = next(((entry, item) for entry, item in reversed(accepted)
                 if item["delivery"]["release_id"] == release), None)
    actions = {}

    def offer(stage, action, label, **options):
        actions.setdefault(stage, []).append({"id": action, "label": label, **options})

    # Every offered next step is also checked by the original command's identity,
    # saved-input, native drain, budget and publication checks.
    stopped = last.get("returncode") != 0
    used = {(a.get("action"), a.get("prior_entry")) for a in attempts if a.get("invoked")}
    if (stopped and not (root / "pipeline-start.json").exists()
            and (not vault.exists() or vault.is_dir() and not any(vault.iterdir()))):
        offer("bootstrap", "start", "重试启动", pause_before=last.get("pause_before"))
    if (stopped and not original and not (native / "operation.json").exists()
            and (root / "pipeline-start.json").is_file()
            and (root / "provisioning-request.json").is_file()
            and (root / "provisioning-receipt.json").is_file()
            and operator.get("node") in {None, "bootstrap", "prepare", "exact"}
            and not (native / "stop.json").exists()
            and source.get("workflow_id") == f"ingest-{request['operation_id']}"
            and source.get("state") not in SOURCE_TERMINAL and not source.get("cancel_requested")):
        prior = read(root / "operator-state.json")
        mailbox = read(root / "operator-pause.json")
        target = None
        if prior.get("contract") == "short-operator-state/v1" and prior.get("operation_id") == request["operation_id"]:
            attempt_id = prior.get("attempt_id")
            target = (mailbox.get("node") if attempt_id and mailbox.get("attempt_id") == attempt_id
                      else prior.get("pending_pause"))
        targets = list(STAGE_ORDER)[STAGE_ORDER["exact"]:]
        if target:
            targets = [node for node in targets if node == target]
        offer(attachment_node, "attach-source", "重新接管同一工作流",
              pause_targets=targets, pending_pause=target)
    if stopped and not original and not release and not short.get("native_active_or_ready"):
        if short.get("last_stage") == "coordination_held":
            offer("coordination", "coordination", "恢复协调")
        if (native / "operation.json").is_file() and any(native.glob("stage-*-writing.json")):
            offer("writing", "writing-replay", "重放已返回的写作")
    authorization = read(root / "formal-authorization.json")
    formal = read(native / "reentries/formal-fifth-release/result.json")
    if stopped and not original and authorization and short.get("last_stage") == "offline_build":
        stage = "release" if formal.get("release_id") else "formal-build"
        if (("delivery", None) not in used
                and (stage == "release" or not (native / "reentries/formal-build-recovery/control.json").exists())):
            offer(stage, "delivery", "恢复交付" if stage == "release" else "恢复正式构建",
                  gateway_required=stage != "release")
    if base:
        entry, item = base
        if entry == "initial":
            if short.get("execution_holds"):
                offer("mapping", "mapping", "按恢复清单重进", manifest=True)
            if item.get("state") == "partial":
                offer("writing", "writing-held", "恢复受阻写作")
            offer("formal-build", "formal-replay", "重进正式构建")
        elif item.get("state") == "partial":
            offer("writing", "writing-held", "恢复受阻写作", prior_entry=entry)
    # A stable CLI entry can consume only the bounded attempts its checkpoint permits.
    return {stage: [action for action in items
                    if action["id"] in {"start", "attach-source", "delivery", "mapping"}
                    or (action["id"], action.get("prior_entry")) not in used]
            for stage, items in actions.items()}


def source_observation(root, source, latest, directory, historical):
    wait = read(root / "source-wait.json")
    if wait.get("workflow_id") not in (None, source.get("workflow_id"), f"ingest-{directory.name}"):
        wait = {}
    if (latest.get("returncode") == 1 and latest.get("action") in {"start", "attach-source"}
            and not wait and not (root / "source-handoff.json").exists()):
        # Old launchers only reported the observer timeout in their attempt log.
        log_path = directory / latest["log"] if historical else directory / "attempts" / latest["id"] / "pipeline.log"
        if "ValueError: native source handoff timed out" in log_tail(log_path):
            wait = {"state": "detached", "stage": "prepare", "updated_at": latest.get("ended_at"),
                    "error": "旧启动器等待整批交接超过 20 分钟后退出。"}
    return wait


def launcher_state(latest, active, source_wait):
    if active:
        return "running"
    if not latest:
        return "pending"
    code = latest.get("returncode")
    if code is None:
        return "interrupted"
    if code == 0:
        return "completed"
    if (code == 75 or (latest.get("action") in {"start", "attach-source", "continue-operation"}
                      and source_wait.get("state") == "detached"
                      and (source_wait.get("updated_at") or 0) >= (latest.get("started_at") or 0))):
        return "detached"
    return "failed"


def operator_state(root, request, latest, active=False):
    status = read(root / "operator-state.json")
    if (status.get("contract") == "short-operator-state/v1"
            and status.get("operation_id") == request["operation_id"]
            and (status.get("attempt_id") == latest.get("id") or
                 (not active and latest.get("action") == "continue-operation" and status.get("state") == "paused"))
            and status.get("node") in STAGE_ORDER):
        return status
    return {}


def progress(directory, *, historical=False, active=False, details=True):
    request = read(directory / "request.json")
    attempts = attempts_for(directory, historical)
    latest = attempts[-1] if attempts else {}
    root = directory / "short"
    native = root / "native"
    node_action = latest.get("action")
    entry_id = latest.get("entry") or latest.get("id", "")
    reentry = native / "reentries" / entry_id
    scoped = node_action in {"mapping", "coordination", "writing-held", "writing-replay"}
    chosen = reentry if scoped else native
    output = reentry if scoped or node_action == "formal-replay" else root
    if node_action == "formal-replay":
        output = native / "reentries" / ("formal-replay-" + latest["id"])
    final = read(output / "final-acceptance.json")
    source = read(ledger_path(request))
    source_wait = source_observation(root, source, latest, directory, historical)
    launcher = launcher_state(latest, active, source_wait)
    operator = operator_state(root, request, latest, active)
    pause_request = read(root / "operator-pause.json") if active and latest.get("operator") else {}
    if pause_request.get("attempt_id") != latest.get("id"):
        pause_request = {}
    pending_pause = operator.get("pending_pause") or latest.get("pause_before")
    if active and not pause_request and pending_pause in STAGE_ORDER:
        pause_request = {"attempt_id": latest["id"], "node": pending_pause}
    if not active and operator.get("state") == "paused" and operator.get("attempt_id") == latest.get("id"):
        launcher = "paused"
    state = ("pausing" if active and pause_request else "running" if active
             else final.get("state", "completed") if final.get("final_acceptance")
             else source["state"] if source.get("state") in {"cancelled", "failed"}
             else "paused" if operator.get("state") == "paused"
             else launcher if launcher != "completed" else "pending")
    data = {"id": request["operation_id"], "vault_name": request["vault_name"],
            "vault_path": request["vault_path"], "state": state,
            "historical": historical, "launcher_state": launcher, "launcher_active": active,
            "source_count": len(request["sources"]),
            "started_at": attempts[0].get("started_at") if attempts else None}
    if not details:
        return data
    provider = read(output / "provider-result.json")
    provision = read(root / "provisioning-receipt.json")
    handoff = read(root / "source-handoff.json")
    qualification = read(root / "source-qualification.json")
    operation = read(native / "operation.json")
    paused = (source.get("pause_control") or {}).get("boundary") or (handoff.get("pause") or {}).get("boundary")
    outcomes = source.get("source_outcomes") or handoff.get("source_outcomes", [])
    counts = Counter(i.get("status") for i in outcomes)
    ready_count, failed_count = counts["ready"], counts["failed"]
    total = len(request["sources"])
    remaining = max(total - ready_count - failed_count, 0)
    source_stage = source.get("current_stage")
    nodes = [{"key": key, "name": name, "description": description, "state": "pending"}
             for key, name, description in STAGES]
    by_key = {n["key"]: n for n in nodes}

    def mark(key, state="completed", **info):
        by_key[key].update(state=state, **info)

    def advance(key):
        for node in nodes:
            if node["key"] == key:
                return
            if node["state"] == "pending":
                node["state"] = "completed"

    if source or handoff or provision.get("phase") in {"sources_staged", "workflow_created"}:
        mark("bootstrap")
    if source or handoff:
        prepare_state = "pending"
        if not remaining:
            prepare_state = "partial" if failed_count and ready_count else "failed" if failed_count else "completed"
        elif active and source_stage in {"created", "source_preparing"} and source_wait.get("active_tasks"):
            prepare_state = "running"
        paths = {item.get("path"): item for item in source_wait.get("files", [])}
        for item in outcomes:
            path = item["path"]
            paths[path] = {**paths.get(path, {}), **item}
        files = []
        for path in request["sources"]:
            name = Path(path).name
            item = paths.get(f"10_Raw/{name}", {})
            files.append({"name": name, "status": item.get("status", "pending"),
                          "error": item.get("reason") or item.get("error_code"),
                          "observed_running_at": item.get("observed_running_at")})
        mark("prepare", prepare_state,
             detail=f"{ready_count} 份就绪 · {failed_count} 份失败 · {remaining} 份未结算",
             sources=files)
    if qualification.get("ok"):
        mark("exact", detail="源工作流停在 exact_plan；已验证短路径交接")
    elif paused:
        mark("exact", "pending", detail=f"源工作流暂停 · {paused}")
    boundary = {"mapping": "mapping", "coordination": "coordination", "writing-held": "writing",
                "writing-replay": "writing", "formal-replay": "formal-build", "delivery": "formal-build"}.get(node_action)
    if boundary:
        previous_prepare = by_key["prepare"]["state"]
        advance(boundary)
        if previous_prepare == "partial":
            by_key["prepare"]["state"] = "partial"
    pending_key = boundary
    planned, held = Counter(), Counter()
    for path in sorted(chosen.glob("stage-*-*.json")):
        match = STAGE_FILE.fullmatch(path.name)
        if not match or match[2] not in STAGE_KEYS:
            continue
        key = STAGE_KEYS[match[2]]
        advance(key)
        metrics = next(iter(sorted(chosen.glob(f"metrics-{match[1]}-*.json"))), None)
        evidence = read(metrics) if metrics else {}
        if match[2] == "coordination_held":
            mark(key, "pending", detail="协调已停住，等待处理")
        elif evidence:
            planned[key] += evidence.get("planned_requests", 0)
            held[key] += evidence.get("held_tasks", 0)
            mark(key, "partial" if held[key] else "completed",
                 detail=f"{planned[key]} 个已处理请求 · {held[key]} 个暂缓")
        else:
            mark(key, "pending")
        pending_key = key

    release = (final.get("delivery") or {}).get("release_id") or (provider.get("index") or {}).get("release_id")
    formal_name = (f"formal-replay-{latest['id']}" if node_action == "formal-replay" else
                   f"formal-{entry_id}" if scoped else "formal-fifth-release")
    formal = read(native / "reentries" / formal_name / "result.json")
    if node_action == "delivery" and not formal:
        formal = read(native / "reentries/formal-build-recovery/result.json")
    release = release or formal.get("release_id")
    indexed = (provider.get("index") or {}).get("status") == "ready"
    if formal.get("run_id") or final.get("final_acceptance") or release:
        advance("formal-build")
        mark("formal-build")
        pending_key = "release"
    if release:
        mark("release", "completed" if indexed or final.get("final_acceptance") else "pending",
             detail=f"发布已应用 · {'索引就绪' if indexed or final.get('final_acceptance') else '等待索引'}")
        if indexed:
            pending_key = "acceptance"
    if final.get("final_acceptance"):
        mark("acceptance", "partial" if final.get("state") == "partial" else "completed",
             detail=f"{final.get('round_published_pages', 0)} 页通过本轮交付核验")
        pending_key = None

    if not pending_key and not final.get("final_acceptance"):
        if source_stage in {"planning", "analyzing"} or handoff:
            pending_key = "exact"
        elif source or by_key["bootstrap"]["state"] == "completed":
            pending_key = "prepare"
        else:
            pending_key = "bootstrap"
    for stage in operator.get("completed", []):
        if stage in by_key and by_key[stage]["state"] == "pending":
            mark(stage)
    operator_node = operator.get("node")
    if operator_node and operator.get("state") in {"running", "paused", "detached", "failed"}:
        if not (operator_node == "prepare" and not remaining and ready_count + failed_count):
            pending_key = operator_node
    failure_at = operator_node if operator.get("state") == "failed" else latest.get("failure_node")
    if (launcher == "failed" and failure_at in by_key and by_key[failure_at]["state"] == "pending"):
        mark(failure_at, "failed", error=operator.get("error"))
    if state == "paused" and operator_node:
        mark(operator_node, "paused", detail="继续时会核对并复用已有成果。"
             if not by_key[operator_node].get("detail") else by_key[operator_node]["detail"])
    observing_source = bool(source and not handoff and source_wait.get("state") == "waiting")
    if (pending_key and active and by_key[pending_key]["state"] == "pending"
            and (not observing_source or source_wait.get("active_tasks"))):
        mark(pending_key, "running")
    if (pause_node := pause_request.get("node")) in by_key and by_key[pause_node]["state"] == "pending":
        mark(pause_node, "pausing", detail="到达此节点前暂停")
    actions = available_actions(root, request, attempts, active, historical, source,
                                "exact" if handoff or source_stage in {"planning", "analyzing"} else "prepare",
                                operator, pause_request)
    for node in nodes:
        node["actions"] = [{**action, "node": node["key"], "attempt": latest.get("id")}
                           for action in actions.get(node["key"], [])]
    vault = Path(request["vault_path"])
    current_release = read(vault / "_system/metadata/knowledge-release-state.json").get("current_release_id")
    current_index = read(vault / "_system/reports/retrieval-index-manifest.json")
    published = current_release or release
    synchronized = (current_index.get("status") == "ready" and current_index.get("release_id") == published
                    if current_index else published == release and (indexed or bool(final.get("final_acceptance"))))
    data.update(nodes=nodes, attempts=attempts, sources=request["sources"], active_node=pending_key,
                workflow_id=f"ingest-{request['operation_id']}", source_stage=source_stage,
                source_revision=source.get("revision", handoff.get("revision")), pause=paused,
                source_wait=source_wait, operator=operator, pause_request=pause_request,
                source_board=(source.get("kanban") or {}).get("board_id"), short_board=operation.get("board"),
                receipts=sum(1 for _ in (native / "receipts").glob("*.json")),
                release_id=published, indexed=synchronized, query_enabled=False)
    return data


def log_tail(path, limit=48000):
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read(limit).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def run_attempt(directory, attempt_id, config, lease_fd):
    path = directory / "attempts" / attempt_id / "attempt.json"
    attempt = read(path)
    code = 1
    failure_node = None
    with path.with_name("pipeline.log").open("a", encoding="utf-8") as log:
        try:
            orch = Path(config["skills_root"]) / ORCHESTRATOR
            bootstrap = orch.parent / BOOTSTRAP
            request_path = directory / "request.json"
            request = read(request_path)
            script = orch / "scripts/start_post_mineru_short_test.py"
            for needed in (script, bootstrap / "scripts/init_obsidian_vault.py", Path(config["provider_config"])):
                if not needed.is_file():
                    raise UserError(f"缺少运行时文件：{needed}")
            sys.path.insert(0, str(orch / "lib"))
            try:
                import hermes_cli
            except ImportError as error:
                raise UserError("请用 Hermes 安装环境中的 Python 启动控制台（见 README）。") from error
            from hermes_source_units.recovery_policy import DEFAULT
            from ingest_kanban import KanbanCLI

            action = attempt["action"]
            # The already-published delivery retry verifies and syncs through
            # program entry points, without starting another native worker.
            formal = read(directory / "short/native/reentries/formal-fifth-release/result.json")
            if not (action == "delivery" and formal.get("release_id")) and not KanbanCLI().dispatcher_available():
                raise UserError("Hermes 原生派发不可用。请在 DevTools 查看 Gateway 状态，然后重试。")
            if action == "start":
                request.setdefault("recovery_policy", DEFAULT)
                save(request_path, request)
            elif action not in {"attach-source", "continue-operation"}:
                verify_drained(request, directory / "short/native")

            command = [sys.executable, "-B", str(script), "--request", str(request_path),
                       "--output", str(directory / "short"), "--provisioner-skill", str(orch),
                       "--bootstrap-skill", str(bootstrap), "--provider-config", config["provider_config"],
                       "--provider-state", config["provider_state"]]
            if attempt.get("operator"):
                command += ["--operator-attempt", attempt_id]
                if action == "attach-source":
                    command += ["--attach-source"]
                elif action == "continue-operation":
                    command += ["--continue-operation"]
                if attempt.get("pause_before"):
                    command += ["--pause-before", attempt["pause_before"]]
            elif action == "delivery":
                command += ["--resume-delivery"]
            elif action != "start":
                node = {"writing-replay": "writing", "writing-held": "writing",
                        "formal-replay": "formal-build"}.get(action, action)
                command += ["--from-node", node, "--reentry-id", attempt_id]
                if action == "writing-replay":
                    command += ["--replay-writing"]
                elif action == "writing-held":
                    command += ["--recover-held-content"]
                    if attempt.get("prior_entry"):
                        command += ["--prior-entry", attempt["prior_entry"]]
                elif action == "mapping":
                    command += ["--mapping-recovery", str(path.with_name("mapping-recovery.json"))]
            print(f"{attempt['label']} · {request['operation_id']}", file=log, flush=True)
            attempt = {**attempt, "invoked": True}
            save(path, attempt)
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False,
                                  pass_fds=(lease_fd,)).returncode
            if attempt.get("operator"):
                status = operator_state(directory / "short", request, attempt, active=True)
                if status.get("state") == "failed":
                    failure_node = status["node"]
            else:
                failure_node = attempt.get("node")
        except Exception as error:
            print(f"{type(error).__name__}: {error}", file=log, flush=True)
        finally:
            record = {**attempt, "returncode": code, "ended_at": time.time()}
            if code not in (0, 75) and failure_node:
                record["failure_node"] = failure_node
            save(path, record)


def verify_drained(request, native):
    from contextlib import closing
    from hermes_cli import kanban_db, kanban_db_connect
    from native_stop_inventory import workflow_processes

    source = read(ledger_path(request))
    origin = source.get("workflow_id") or f"ingest-{request['operation_id']}"
    if source.get("cancel_requested") or (source.get("pause_control") or {}).get("boundary") != "exact_plan":
        raise UserError("源工作流必须保留已验证的 exact_plan 暂停。")
    source_board = (source.get("kanban") or {}).get("board_id")
    short_board = read(native / "operation.json").get("board")
    if not source_board or not short_board:
        raise UserError("缺少已绑定的原生看板，请先检查现有运行证据。")
    for board in (source_board, short_board):
        if workflow_processes(origin, board):
            raise UserError(f"原生看板 {board} 仍有活动进程。")
        with closing(kanban_db_connect.connect(board=board)) as connection:
            blocked = {"running", "ready", "triage"} if board == source_board else {"running", "ready", "triage", "todo"}
            if any(task.claim_lock or task.status in blocked for task in kanban_db.list_tasks(connection)):
                raise UserError(f"原生看板 {board} 尚未排空。")
