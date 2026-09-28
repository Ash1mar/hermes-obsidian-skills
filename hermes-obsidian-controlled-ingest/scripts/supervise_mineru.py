#!/usr/bin/env python3
"""Skill-owned Linux admission and process supervision; no system service required."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

TAG = "HERMES_SKILL_MINERU_RUN"
STOP = False


def identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def alive(pid, start):
    return start is not None and identity(pid) == str(start)


def tagged(token):
    found = {}
    match = f"{TAG}={token}".encode()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if match in (path / "environ").read_bytes().split(b"\0"):
                start = identity(int(path.name))
                if start is not None:
                    found[int(path.name)] = start
        except (OSError, PermissionError):
            continue
    return found


def cleanup(record, grace):
    """Kill only this run's tagged descendants, including detached descendants."""
    for sig, seconds in ((signal.SIGTERM, grace), (signal.SIGKILL, 3)):
        deadline = time.monotonic() + seconds
        while True:
            targets = tagged(record["token"])
            if not targets:
                return True
            for pid, start in targets.items():
                if alive(pid, start):
                    try:
                        group = os.getpgid(pid)
                        if group != os.getpgrp():
                            os.killpg(group, sig)
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    return not tagged(record["token"])


@contextmanager
def admission_lock(root):
    import fcntl
    with (root / "admission.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def available_memory_mib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable is unavailable; refusing local MinerU startup")


def gpu_ready(minimum, reservations=0):
    if not minimum:
        return True
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                                 "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return True  # CPU-only hosts need no NVIDIA admission check.
    except subprocess.TimeoutExpired:
        return False
    values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    return result.returncode == 0 and bool(values) and all(
        value - reservations >= minimum for value in values)


def check_binding(vault, binding):
    if binding is None:
        if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") or os.environ.get("HERMES_KANBAN_TASK"):
            raise RuntimeError("isolated conversion requires --vault and --worker-binding")
        return
    if vault is None or not all(isinstance(binding.get(key), str) and binding[key]
                                for key in ("workflow_id", "task_id", "node")):
        raise RuntimeError("conversion binding needs vault, workflow_id, task_id and node")
    workflow_id = binding["workflow_id"]
    if not workflow_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in workflow_id):
        raise RuntimeError("invalid workflow id")
    value = json.loads((Path(vault) / "_system/ledgers/ingest-workflows" /
                        f"{workflow_id}.json").read_text())
    if (value.get("cancel_requested") or value.get("batch_id") is not None
            or value.get("current_stage") not in ("created", "source_preparing")):
        raise RuntimeError("workflow stopped or left source preparation")
    if not binding["node"].startswith("source-prepare:") or not any(
            item.get("task_id") == binding["task_id"] and item.get("node") == binding["node"]
            for item in value["kanban"]["task_map"]):
        raise RuntimeError("conversion card was superseded")


def stopped(*_):
    global STOP
    STOP = True


def supervise(command, config, root, caller_pid, caller_start, vault=None, binding=None):
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    record = None
    slot = None
    process = None
    began = time.monotonic()
    try:
        while slot is None:
            if STOP or not alive(caller_pid, caller_start):
                raise RuntimeError("conversion caller exited while waiting")
            check_binding(vault, binding)
            if time.monotonic() - began > config["queue_timeout_seconds"]:
                raise TimeoutError("MinerU admission timed out")
            with admission_lock(root):
                reservations = 0
                occupied = set()
                for path in root.glob("slot-*.json"):
                    prior = json.loads(path.read_text())
                    if prior["boot"] != boot:
                        path.unlink()
                    elif not alive(prior["supervisor_pid"], prior["supervisor_start"]):
                        if not cleanup(prior, config["terminate_grace_seconds"]):
                            raise RuntimeError("orphan MinerU descendants could not be cleaned")
                        path.unlink()
                    else:
                        occupied.add(path.stem)
                        if time.time() - prior["started_at"] < config["startup_reservation_seconds"]:
                            reservations += config["startup_reservation_mib"]
                ready = (len(occupied) < config["max_concurrent"]
                         and available_memory_mib() - reservations >= config["min_available_memory_mib"]
                         and gpu_ready(config["min_free_gpu_memory_mib"], reservations))
                if ready:
                    for index in range(config["max_concurrent"]):
                        if f"slot-{index}" in occupied:
                            continue
                        candidate = (root / f"slot-{index}.lock").open("a+")
                        try:
                            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            candidate.close()
                            continue
                        slot = candidate
                        record = {"token": uuid.uuid4().hex, "boot": boot,
                                  "supervisor_pid": os.getpid(), "supervisor_start": identity(os.getpid()),
                                  "started_at": time.time(), "slot": index}
                        record_path = root / f"slot-{index}.json"
                        record_path.write_text(json.dumps(record))
                        break
            if slot is None:
                time.sleep(config["poll_seconds"])
        check_binding(vault, binding)
        if STOP or not alive(caller_pid, caller_start):
            raise RuntimeError("conversion caller exited before launch")
        env = {**os.environ, TAG: record["token"]}
        process = subprocess.Popen(command, env=env, start_new_session=True,
                                   pass_fds=(slot.fileno(),))
        print(json.dumps({"mineru_started": process.pid, "slot": record["slot"]}),
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + config["timeout_seconds"]
        while process.poll() is None:
            if STOP or not alive(caller_pid, caller_start):
                raise RuntimeError("conversion caller exited or conversion interrupted")
            check_binding(vault, binding)
            if time.monotonic() > deadline:
                raise TimeoutError("MinerU conversion timed out")
            time.sleep(config["poll_seconds"])
        return process.returncode
    finally:
        if record is not None:
            cleaned = cleanup(record, config["terminate_grace_seconds"])
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    cleaned = False
            if cleaned:
                with admission_lock(root):
                    record_path.unlink(missing_ok=True)
            slot.close()
            if not cleaned:
                raise RuntimeError("MinerU cleanup incomplete; record retained for recovery")


def main():
    if sys.platform != "linux":
        raise RuntimeError("local supervised MinerU requires Linux/WSL")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/mineru-runtime.json")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".cache/hermes-skill-runtime/mineru")
    parser.add_argument("--caller-pid", type=int, required=True)
    parser.add_argument("--caller-start", required=True)
    parser.add_argument("--vault")
    parser.add_argument("--worker-binding", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    for key, value in config.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"invalid MinerU runtime setting: {key}")
    if config["max_concurrent"] not in (1, 2) or config["poll_seconds"] <= 0:
        raise ValueError("MinerU concurrency must be 1 or 2 and poll interval positive")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("missing MinerU command")
    binding = json.loads(args.worker_binding.read_text()) if args.worker_binding else None
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stopped)
    return supervise(command, config, args.state_dir, args.caller_pid,
                     args.caller_start, args.vault, binding)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"MinerU supervisor: {exc}", file=sys.stderr)
        sys.exit(1)
