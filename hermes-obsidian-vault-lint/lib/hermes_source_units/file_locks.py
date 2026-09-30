"""Host kernel locks with Vault owner markers; legacy recovery is explicit."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid

from .validation import ContractError

CONTRACT = "hermes-skill-kernel-lock/v1"


def host_identity():
    machine = Path("/etc/machine-id")
    name = machine.read_text().strip() if os.name == "posix" and machine.exists() else socket.gethostname()
    return f"{os.name}:{name}"


def owner_record():
    boot = Path("/proc/sys/kernel/random/boot_id")
    return {"contract": CONTRACT, "pid": os.getpid(), "host": host_identity(),
            "boot_id": boot.read_text().strip() if boot.exists() else None,
            "token": uuid.uuid4().hex}


def process_exists(pid):
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            checked = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            kernel.CloseHandle(ctypes.c_void_p(handle))
            return not checked or code.value == 259
        return ctypes.get_last_error() != 87  # Only ERROR_INVALID_PARAMETER proves absence.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def kernel_lock(path, *, timeout=0, check=None):
    """Stable host-local inode; never unlink it while a contender can open it."""
    root = (Path.home() / ".cache/hermes-skill-runtime/locks" if os.name == "posix"
            else Path(tempfile.gettempdir()) / "hermes-skill-runtime-locks")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()
    deadline = time.monotonic() + timeout
    def busy():
        if check is not None:
            check()
        remaining = deadline - time.monotonic()
        if timeout <= 0 or remaining <= 0:
            code = "LOCK_TIMEOUT" if timeout > 0 else "LOCK_BUSY"
            raise ContractError(code, "$", f"Skill kernel lock is held: {path}; wait limit={timeout}s")
        time.sleep(min(.05, remaining))
    with (root / f"{key}.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    busy()
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    busy()
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path, *, timeout=0, check=None):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with kernel_lock(path, timeout=timeout, check=check):
        if path.exists():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                prior = None
            if (not isinstance(prior, dict) or prior.get("contract") != CONTRACT
                    or prior.get("host") != host_identity()):
                raise ContractError("LOCK_RECOVERY_REQUIRED", "$",
                                    f"Legacy or foreign-host lock requires supported recovery: {path}")
        # An acquired kernel lock proves no updated local owner still holds it,
        # even when an OOM kill left its marker behind.
        record = owner_record()
        from .source_units import _write_atomic, _json_bytes
        _write_atomic(path, _json_bytes(record))
        try:
            yield
        finally:
            # The kernel inode is separate and remains stable after marker removal.
            if path.exists():
                current = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(current, dict) and current.get("token") == record["token"]:
                    path.unlink()
