"""Open a known Vault in the WSL desktop application."""

import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
from urllib.parse import urlencode

from workflow import UserError, log_tail


def desktop_user(configured=None):
    if configured:
        user = pwd.getpwnam(configured)
    elif os.geteuid() != 0:
        user = pwd.getpwuid(os.geteuid())
    else:
        candidates = [user for user in pwd.getpwall() if 1000 <= user.pw_uid < 65534
                      and Path(user.pw_dir).is_dir()]
        if len(candidates) != 1:
            raise UserError("请用 --obsidian-user 指定 WSL 图形应用用户。")
        user = candidates[0]
    if user.pw_uid == 0:
        raise UserError("请为 Obsidian 指定普通 Linux 用户。")
    return user


def open_obsidian(vault, action, state, configured_user=None):
    if action not in {"manager", "open"}:
        raise UserError("请选择打开 Vault 或 Vault 管理器。")
    executable = shutil.which("obsidian")
    if not executable:
        raise UserError("WSL 中尚未安装 Obsidian。")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise UserError("WSL 图形会话不可用，请确认 WSLg 已启动。")
    vault = Path(vault).resolve(strict=True)
    if not vault.is_dir() or not (vault / "_system/vault.json").is_file():
        raise UserError("Vault 尚未创建完成。")
    user = desktop_user(configured_user)
    uri = "obsidian://choose-vault"
    if action == "open":
        note = vault / "README.md"
        if not note.is_file():
            raise UserError("找不到 Vault 的 README.md。")
        uri = "obsidian://open?" + urlencode({"path": str(note)})
    environment = {**os.environ, "HOME": user.pw_dir, "USER": user.pw_name,
                   "LOGNAME": user.pw_name}
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        environment.pop(key, None)
    runtime = Path("/run/user") / str(user.pw_uid)
    if os.geteuid() == 0:
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(runtime, user.pw_uid, user.pw_gid)
        runtime.chmod(0o700)
        settings = vault / ".obsidian"
        if settings.is_symlink():
            raise UserError("Obsidian 设置目录不能是符号链接。")
        settings.mkdir(exist_ok=True)
        # The viewer can persist its workspace while domain files retain their owners.
        result = subprocess.run(["setfacl", "-R", "-m", f"u:{user.pw_uid}:rwX", str(settings)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise UserError("无法授予图形用户写入 Obsidian 设置的权限。")
        for directory in (settings, *(p for p in settings.rglob("*") if p.is_dir() and not p.is_symlink())):
            subprocess.run(["setfacl", "-m", f"d:u:{user.pw_uid}:rwx", str(directory)],
                           check=True, capture_output=True, timeout=10)
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if wayland and not wayland.startswith("/"):
        socket = Path("/mnt/wslg/runtime-dir") / wayland
        if socket.exists():
            environment["WAYLAND_DISPLAY"] = str(socket)
    command = [executable, uri]
    if os.geteuid() == 0:
        command = ["runuser", "-u", user.pw_name, "--", *command]
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = state / "obsidian.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(0.5)
    if process.poll() not in (None, 0):
        print(log_tail(log_path, 2000), flush=True)
        raise UserError("Linux Obsidian 启动失败，请检查控制台的 obsidian.log。")
    return {"opened": True, "user": user.pw_name, "vault_path": str(vault), "action": action}
