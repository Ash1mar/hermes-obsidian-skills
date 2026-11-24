"""Fence native consumers and clean the boards owned by a console run."""

from contextlib import ExitStack, closing, contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from incremental import fingerprint
from workflow import ORCHESTRATOR, UserError, read


BOARD = re.compile(r"(?:ingest-[0-9a-f]{24}|incremental-[0-9a-f]{32}|short-console-[0-9a-f]{20})")
BUSY = {"ready", "running", "todo", "triage", "review"}


def local_record(path):
    if path.resolve() != path or (path.exists() and not path.is_file()):
        raise UserError("原生运行记录路径已变化，无法核对删除范围。")
    return read(path)


def valid_boards(boards):
    if not isinstance(boards, list):
        raise UserError("原生看板标识无效，无法安全删除。")
    slugs = set()
    for row in boards:
        if not isinstance(row, dict) or set(row) != {"slug", "identity"}:
            raise UserError("原生看板清理状态无效。")
        slug, identity = row["slug"], row["identity"]
        if not isinstance(slug, str) or not BOARD.fullmatch(slug) or slug in slugs:
            raise UserError("原生看板标识无效，无法安全删除。")
        if identity is not None and (not isinstance(identity, list) or len(identity) != 2
                                     or any(type(value) is not int for value in identity)):
            raise UserError("原生看板目录身份无效。")
        slugs.add(slug)
    return slugs


def incremental_board(native, workspace, run_id):
    parts = workspace.relative_to(native).parts
    if len(parts) not in (3, 5) or (len(parts) == 5 and parts[3] != "recoveries"):
        return None
    kind, scope_id, stage = parts[:3]
    recovery = parts[4] if len(parts) == 5 else None
    return "incremental-" + fingerprint([run_id, kind, scope_id, stage, recovery])[:32]


def look_for_boards(kb, vault, directory, request, root, workflow_id):
    source_root = vault / "_system/ledgers/ingest-workflows"
    source_path = source_root / (workflow_id + ".json")
    source = local_record(source_path)
    if source and (source.get("workflow_id") != workflow_id or source.get("actor") != request["actor"]):
        raise UserError("来源工作流身份与控制台记录不一致。")

    source_board = source.get("kanban", {}).get("board_id")
    scoped_board = "ingest-" + fingerprint(f"{vault}:{workflow_id}")[:24]
    legacy_board = "ingest-" + fingerprint(workflow_id)[:24]
    if source_board and source_board not in {scoped_board, legacy_board}:
        raise UserError("来源看板与工作流绑定不一致。")

    if root is not None:
        if source and source.get("scope", {}).get("incremental_run_id") != directory.name:
            raise UserError("来源工作流未绑定到这条增量运行。")
        run = local_record(root / "run.json")
        if run and (run.get("run_id") != directory.name or run.get("actor") != request["actor"]
                    or run.get("source_workflow_id") != workflow_id):
            raise UserError("增量运行身份与控制台记录不一致。")
        native = root / "native"
    else:
        native = directory / "short/native"
    if native.resolve() != native:
        raise UserError("原生工作目录不能使用链接。")

    boards = {source_board} if source_board else set()
    known = {row["slug"]: row for row in kb.list_boards()}
    # A launch can create a board immediately before persisting its receipt.
    if source and not source_board:
        boards.update({scoped_board, legacy_board} & known.keys())
    if root is None:
        boards.update({"short-" + directory.name} & known.keys())
    else:
        for slug, row in known.items():
            workdir = row.get("default_workdir")
            if slug.startswith("incremental-") and workdir:
                workspace = Path(workdir)
                if workspace.is_relative_to(native) and workspace.resolve() == workspace:
                    if slug == incremental_board(native, workspace, directory.name):
                        boards.add(slug)
    for path in native.rglob("operation.json"):
        record = local_record(path)
        board = record.get("board")
        origin = record.get("origin", {})
        if (record.get("contract") != "native-short-evidence-test/v1"
                or record.get("vault") != str(vault) or origin.get("workflow_id") != workflow_id):
            raise UserError("原生工作目录的来源身份不一致。")
        if root is not None:
            scope = record.get("incremental", {})
            expected = incremental_board(native, path.parent, directory.name)
            if expected is None:
                raise UserError("原生增量看板的工作目录不一致。")
            if (record.get("workflow_mode") != "incremental_v1" or record.get("actor") != request["actor"]
                    or [scope.get(key) for key in ("run_id", "scope_kind", "scope_id", "stage")]
                    != [directory.name, *path.parent.relative_to(native).parts[:3]]):
                raise UserError("原生增量看板的运行范围不一致。")
        else:
            expected = "short-" + directory.name
        if board != expected:
            raise UserError("原生看板标识与运行记录不一致。")
        boards.add(board)
    return source, sorted(boards)


def ensure_controller_drained(root, request):
    head = local_record(root / "control/head.json")
    if head and (head.get("run_id") != request["operation_id"] or head.get("actor") != request["actor"]):
        raise UserError("增量控制身份与运行记录不一致。")
    if head.get("active_scopes"):
        raise UserError("增量运行仍有未退出的处理任务，请先停止运行再删除。")
    owner = local_record(root / "controller.json")
    if not owner:
        return
    pid = owner.get("pid")
    if type(pid) is not int or pid < 1:
        raise UserError("无法核对增量控制进程的身份。")
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return
    if fields[0] not in {"Z", "X"} and fields[19] == owner.get("start"):
        raise UserError("增量控制进程仍在运行，请先停止运行再删除。")


def board_directory(kb, board):
    path = kb.board_dir(board).absolute()
    db = path / "kanban.db"
    if (path.name != board or path.resolve() != path
            or (path.exists() and not path.is_dir())
            or kb.kanban_db_path(board=board).absolute() != db
            or db.resolve() != db or (db.exists() and not db.is_file())):
        raise UserError(f"原生看板使用了非标准目录：{board}")
    return path


def board_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise UserError(f"原生看板不再是实际目录：{path.name}")
    return [info.st_dev, info.st_ino]


@contextmanager
def hold_board(path):
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path / "kanban.db.dispatch.lock", flags, 0o600), "r+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UserError("无法核对原生看板的派发锁。")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UserError("原生看板正在核对派发状态，请稍后重试。") from error
        yield


def ensure_board_drained(kb, path):
    database = path / "kanban.db"
    if not database.exists():
        return
    # Normal Kanban connections initialize missing databases. A deletion check
    # must never create or migrate a board, including during a retry.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as connection:
        connection.row_factory = sqlite3.Row
        if (any(task.claim_lock or task.status in BUSY for task in kb.list_tasks(connection, include_archived=True))
                or connection.execute("SELECT 1 FROM task_runs WHERE ended_at IS NULL LIMIT 1").fetchone()):
            raise UserError(f"原生看板尚有可派发任务或活动租约：{path.name}。请先停止此运行。")


@contextmanager
def hold_drained(vault, directory, request, config, boards=None):
    skill_lib = str(Path(config["skills_root"]) / ORCHESTRATOR / "lib")
    if skill_lib not in sys.path:
        sys.path.append(skill_lib)
    from hermes_source_units.file_locks import exclusive_lock
    from hermes_source_units.knowledge_build import FileKnowledgeBuildService
    from hermes_source_units.validation import ContractError
    from native_stop_inventory import workflow_processes

    work_id = "ingest-" + directory.name
    root = (vault / "_system/ledgers/incremental-ingest" / directory.name
            if request.get("workflow_mode") == "incremental_v1" else None)
    source_root = vault / "_system/ledgers/ingest-workflows"
    lock_paths = ([root / ".controller-start.lock", root / ".controller.lock", root / ".runtime-mutation.lock"]
                  if root is not None and root.is_dir() else [])
    if root is not None and (root / "control").is_dir():
        lock_paths.append(root / "control/.lock")
    if root is None and (directory / "short/native").is_dir():
        lock_paths.append(directory / "short/native/controller.lock")
    lock_paths.extend(source_root / folder / (work_id + ".lock")
                      for folder in (".dispatch-locks", ".locks") if (source_root / folder).is_dir())
    with ExitStack() as stack:
        try:
            for path in lock_paths:
                if path.resolve() != path:
                    raise UserError("工作流锁路径不能使用链接。")
                stack.enter_context(exclusive_lock(path, timeout=0, marker=False))
        except ContractError as error:
            raise UserError("工作流仍被执行器使用或其锁状态待核对，请先停止运行。") from error

        if root is not None:
            ensure_controller_drained(root, request)
        try:
            from hermes_cli import kanban_db as kb
        except ImportError as error:
            raise UserError("当前控制台无法核对 Hermes 原生看板，请在 Hermes 运行环境中重试。") from error

        if boards is None:
            source, observed = look_for_boards(kb, vault, directory, request, root, work_id)
            boards = [{"slug": board, "identity": board_identity(board_directory(kb, board))} for board in observed]
            if source.get("batch_id"):
                slices = FileKnowledgeBuildService(vault)._slices(source["batch_id"])
                if any(item["state"] == "leased" or item["lease"]["worker_id"] for item in slices):
                    raise UserError("来源批次仍有运行租约，请先停止后再删除。")

        slugs = valid_boards(boards)
        for row in boards:
            board = row["slug"]
            path = board_directory(kb, board)
            observed = board_identity(path)
            if observed is None:
                continue
            if observed != row["identity"]:
                raise UserError(f"原生看板目录身份已经变化：{board}")
            stack.enter_context(hold_board(path))
            try:
                ensure_board_drained(kb, path)
            except sqlite3.Error as error:
                raise UserError(f"原生看板状态暂时无法核对：{board}。请稍后重试。") from error
        try:
            if any(workflow_processes(work_id, board) for board in slugs or {""}):
                raise UserError("工作流仍有原生进程或监视器，请先停止运行再删除。")
        except ContractError as error:
            raise UserError("无法核对工作流的原生进程，请稍后重试。") from error
        yield boards


def remove_boards(boards, config):
    """Use the supported native hard deletion while the caller holds the scopes."""
    valid_boards(boards)
    if not boards:
        return
    from hermes_cli import kanban_db as kb

    for row in boards:
        board = row["slug"]
        path = board_directory(kb, board)
        observed = board_identity(path)
        if observed is None:
            continue
        if observed != row["identity"]:
            raise UserError(f"原生看板目录身份已经变化：{board}")
        kb.remove_board(board, archive=False)
        if path.exists():
            raise UserError(f"原生看板在清理期间重新出现：{board}。请重试删除。")
