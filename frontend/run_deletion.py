"""Permanently remove one console-owned run and its private Vault."""

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat

from run_deletion_native import hold_drained, remove_boards
from workflow import UserError, attempts_for, read, save


OWNED_ID = re.compile(r"console-[0-9a-f]{20}")


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def tree_identity(path):
    if not path.is_absolute() or path == path.parent or path.resolve() != path:
        raise UserError("删除目标必须是实际目录，不能使用链接。")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise UserError("删除目标已经不是目录。")
    return [info.st_dev, info.st_ino]


def check_mounts(path):
    # Recursive removal must never enter a mounted tree, including a bind mount
    # on the same device. An ancestor mount (for example /mnt/c) is fine.
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        mount = re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), line.split()[4])
        if Path(os.fsdecode(mount)).is_relative_to(path):
            raise UserError(f"删除目标包含挂载目录：{path}")


@contextmanager
def hold_provider(path):
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path / "sync.lock", flags, 0o600), "r+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UserError("无法核对该 Vault 的 Provider 同步锁。")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UserError("此 Vault 的索引正在同步，请等同步结束后再删除。") from error
        yield


class RunDeletion:
    def __init__(self, config):
        self.config = config
        self.state = Path(config["state"])
        self.journal = self.state / ".deletions"

    def pending(self, run_id):
        return read(self.journal / f"{run_id}.json")

    def pending_rows(self):
        return {path.stem: plan["row"] for path in self.journal.glob("*.json") if (plan := read(path))}

    def check_new_request(self, request):
        for row in self.pending_rows().values():
            pending = Path(row["vault_path"])
            if overlaps(pending, Path(request["vault_path"])) or any(
                    Path(source).is_relative_to(pending) for source in request["sources"]):
                raise UserError("路径与一个尚未完成删除的 Vault 重叠。")

    def provider_paths(self, request, vault):
        # The console default also applies to older runs. A local adapter can
        # override it directly or via the Provider's own configuration.
        adapter_path = request.get("incremental_provider_config") or self.config.get("provider_config")
        adapter = read(Path(adapter_path)) if adapter_path else {}
        host_path = adapter.get("provider_config")
        host = read(Path(host_path).expanduser()) if host_path else {}
        release_path = vault / "_system/reports/retrieval-index-manifest.json"
        if release_path.resolve() != release_path:
            raise UserError("Provider 索引状态文件不能使用链接。")
        release = read(release_path)
        if release.get("transport") == "http" and release.get("status") == "ready":
            raise UserError("该 Vault 有远程 Provider 索引，请先从 Provider 主机清理并核对索引状态。")
        # Match the path-derived namespace used by qmd_like_rag.config.stable_vault_id.
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", vault.name).strip("-") or "vault"
        default_id = f"{stem}-{sha256(vault.as_posix().encode('utf-8')).hexdigest()[:12]}".lower()
        configured_id = adapter.get("vault_id") or host.get("vault_id") or default_id
        owned_ids = {request["vault_id"], default_id}
        if configured_id not in owned_ids or (release.get("vault_id") and release["vault_id"] not in owned_ids):
            raise UserError("Provider 的 Vault 身份与运行记录不一致。")
        console_root = Path(self.config["provider_state"])
        paths = {console_root / request["vault_id"]}
        if adapter.get("transport", "command") == "command":
            xdg = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state").expanduser()
            local_root = (adapter.get("state_root") or host.get("state_root")
                          or os.environ.get("QMD_LIKE_RAG_STATE_ROOT") or xdg / "qmd-like-rag")
            paths.add(Path(local_root).expanduser() / configured_id)
        return sorted(paths)

    def validate_ownership(self, directory, request, vault):
        run_id = directory.name
        if (not OWNED_ID.fullmatch(run_id) or request.get("operation_id") != run_id
                or request.get("vault_id") != run_id or request.get("security_domain") != "internal-test"):
            raise UserError("无法证明 Vault 专属于这条控制台运行记录。")
        if overlaps(vault, self.state):
            raise UserError("Vault 路径与控制台状态目录重叠。")
        if tree_identity(vault) is None or not any(vault.iterdir()):
            return
        manifest_path = vault / "_system/vault.json"
        if manifest_path.resolve() != manifest_path:
            raise UserError("Vault 身份文件不能使用链接。")
        identity = read(manifest_path).get("vault", {})
        if identity.get("id") != run_id or identity.get("security_domain", "internal-test") != "internal-test":
            raise UserError("Vault 身份与运行记录不一致，请核对目录。")
        ledgers = vault / "_system/ledgers"
        if (any(path.stem != f"ingest-{run_id}" for path in (ledgers / "ingest-workflows").glob("*.json"))
                or any(path.is_dir() and path.name != run_id for path in (ledgers / "incremental-ingest").glob("*"))):
            raise UserError("该 Vault 包含其他工作流，无法从这条记录删除。")

    def validate_scope(self, paths, request, other_records):
        originals = [Path(raw).resolve() for raw in request["sources"]]
        for record in other_records:
            other = read(record)
            if other:
                other_vault = Path(other["vault_path"]).resolve()
                if any(overlaps(path, other_vault) for path in paths):
                    raise UserError("删除目标与另一条运行的 Vault 重叠。")
                originals.extend(Path(raw).resolve() for raw in other["sources"])
            if any(record.resolve().is_relative_to(path) for path in paths):
                raise UserError("删除目标包含另一条运行记录。")
        if any(source.is_relative_to(path) for source in originals for path in paths):
            raise UserError("删除目标包含运行的外部输入原件。")
        for path in paths:
            if any(other != path and overlaps(path, other) for other in paths):
                raise UserError("删除目标目录彼此重叠。")

    def plan(self, directory, request, vault):
        self.validate_ownership(directory, request, vault)
        providers = self.provider_paths(request, vault)
        if any(overlaps(path, self.state) for path in providers):
            raise UserError("Provider 目录与控制台状态目录重叠。")
        trees = {}
        for path in [*providers, vault, directory]:
            trees[str(path)] = tree_identity(path)
        attempts = attempts_for(directory)
        row = {"id": directory.name, "vault_name": request["vault_name"], "vault_path": str(vault),
               "workflow_mode": request.get("workflow_mode"), "historical": False, "launcher_active": False,
               "source_count": len(request["sources"]), "started_at": attempts[0].get("started_at") if attempts else None,
               "state": "paused", "deletion_pending": True, "attempts": [], "nodes": []}
        return {"request": request, "row": row, "trees": trees, "providers": list(map(str, providers))}

    def delete(self, directory, confirmation, other_records):
        journal = self.journal / f"{directory.name}.json"
        pending = self.pending(directory.name)
        request = pending["request"] if pending else read(directory / "request.json")
        if not request:
            raise FileNotFoundError(directory.name)
        if not isinstance(confirmation, str) or confirmation != request["vault_name"]:
            raise UserError("请输入与运行记录完全一致的 Vault 名称。")
        vault = Path(request["vault_path"])
        try:
            if not pending and tree_identity(directory) is None:
                raise FileNotFoundError(directory.name)
            plan = pending or self.plan(directory, request, vault)
            # Keep directory ownership pinned across filesystem failures. If a
            # path has since been reused, leave the replacement untouched.
            paths = [Path(path) for path in plan["trees"]]
            self.validate_scope(paths, request, other_records)
            existing = []
            for path in paths:
                observed = tree_identity(path)
                if observed is None:
                    continue
                if plan["trees"][str(path)] != observed:
                    raise UserError(f"删除目标的目录身份已经变化：{path}")
                check_mounts(path)
                existing.append(path)
            if pending and (vault / "_system/vault.json").exists():
                self.validate_ownership(directory, request, vault)
            with ExitStack() as stack:
                boards = stack.enter_context(hold_drained(vault, directory, request, self.config,
                                                          boards=plan.get("boards")))
                for path in map(Path, plan["providers"]):
                    if path in existing:
                        stack.enter_context(hold_provider(path))
                if not pending:
                    self.journal.mkdir(mode=0o700, exist_ok=True)
                    save(journal, {**plan, "boards": boards})
                remove_boards(boards, self.config)
                for path in existing:
                    shutil.rmtree(path)
                active = self.state / "active.json"
                if read(active).get("run") == directory.name:
                    active.unlink(missing_ok=True)
                journal.unlink()
        except OSError as error:
            raise UserError(f"无法完成删除。请检查目录是否仍被使用后重试（{error.strerror or error}）。") from error
