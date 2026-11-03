"""Host-local synchronization records and a cross-process single-writer lock."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def fingerprint(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def _lock(stream: Any, acquire: bool) -> None:
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def writer_lock(persist_dir: Path) -> Iterator[None]:
    persist_dir.mkdir(parents=True, exist_ok=True)
    # Never unlink the lock inode: CLI and HTTP writers must lock the same file.
    with (persist_dir / "sync.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        try:
            _lock(stream, True)
        except OSError as exc:
            raise RuntimeError("Provider sync already running for this Vault") from exc
        try:
            yield
        finally:
            _lock(stream, False)


def writer_active(persist_dir: Path) -> bool:
    try:
        stream = (persist_dir / "sync.lock").open("rb")
    except FileNotFoundError:
        return False
    with stream:
        try:
            _lock(stream, True)
        except OSError:
            return True
        _lock(stream, False)
    return False


def bounded_batches(documents: list[dict[str, Any]], max_rows: int,
                    max_tokens: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    tokens = 0
    for row in documents:
        cost = int(row["token_count"])
        if cost > max_tokens:
            raise RuntimeError("One projection exceeds embedding_batch_max_tokens; increase the batch budget")
        if batch and (len(batch) >= max_rows or tokens + cost > max_tokens):
            yield batch
            batch, tokens = [], 0
        batch.append(row)
        tokens += cost
    if batch:
        yield batch
