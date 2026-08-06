from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODING = None


def count_tokens(text: str) -> int:
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return len(re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]", text))


def stable_chunk_id(source: str, start_line: int, end_line: int, heading: str, text: str) -> str:
    value = "|".join([source, str(start_line), str(end_line), heading, text])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def detect_chunk_type(text: str, path: Path) -> str:
    lowered = text.casefold()
    if "backlinks" in lowered or "反向链接" in text:
        return "backlink"
    if text.count("[[") > 5 and len(text) < 300:
        return "navigation"
    if path.name.casefold().endswith(".source-map.md") or path.name.casefold().endswith(".spec-index.md"):
        return "index"
    if text.startswith("---"):
        return "metadata"
    return "normal"


def parse_heading(line: str) -> tuple[int, str | None]:
    match = re.match(r"^(#{1,6})\s+(.*)", line)
    if not match:
        return 0, None
    return len(match.group(1)), match.group(2).strip()


def split_child(text: str, size: int, overlap: float) -> list[str]:
    if count_tokens(text) <= size:
        return [text]
    if _ENCODING is not None:
        tokens = _ENCODING.encode(text)
        step = max(1, int(size * (1 - overlap)))
        return [_ENCODING.decode(tokens[index : index + size]) for index in range(0, len(tokens), step)]
    units = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]|\s+|[^\w\s]", text)
    step = max(1, int(size * (1 - overlap)))
    return ["".join(units[index : index + size]).strip() for index in range(0, len(units), step)]


def chunk_markdown_file(
    path: Path,
    *,
    source_id: str,
    source_sha256: str,
    chunk_size: int = 800,
    overlap_ratio: float = 0.15,
) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parents: list[dict[str, Any]] = []
    current: list[str] = []
    start_line = 1
    heading_path: list[str] = []
    parent_number = 0

    def save_parent(end_line: int) -> None:
        nonlocal current, parent_number
        content = "\n".join(current).strip()
        if content:
            parent_id = stable_chunk_id(source_id, start_line, end_line, " > ".join(heading_path), content)
            parents.append(
                {
                    "parent_id": parent_id,
                    "parent_text": content,
                    "source": source_id,
                    "source_sha256": source_sha256,
                    "start_line": start_line,
                    "end_line": end_line,
                    "heading": " > ".join(heading_path),
                    "parent_number": parent_number,
                }
            )
            parent_number += 1
        current = []

    for line_number, line in enumerate(lines, start=1):
        level, title = parse_heading(line)
        if level:
            if current:
                save_parent(line_number - 1)
            heading_path = heading_path[: level - 1] + [title or ""]
            start_line = line_number
        current.append(line)
    if current:
        save_parent(len(lines))

    chunks: list[dict[str, Any]] = []
    for parent in parents:
        chunk_type = detect_chunk_type(parent["parent_text"], path)
        for chunk_number, child in enumerate(split_child(parent["parent_text"], chunk_size, overlap_ratio)):
            if not child:
                continue
            chunks.append(
                {
                    "id": stable_chunk_id(
                        source_id,
                        parent["start_line"],
                        parent["end_line"],
                        parent["heading"],
                        child,
                    ),
                    "chunk_no": chunk_number,
                    "type": "child",
                    "chunk_type": chunk_type,
                    "text": child,
                    "parent_id": parent["parent_id"],
                    "parent_text": parent["parent_text"],
                    "source": source_id,
                    "source_sha256": source_sha256,
                    "start_line": parent["start_line"],
                    "end_line": parent["end_line"],
                    "heading": parent["heading"],
                    "tokens": count_tokens(child),
                }
            )
    return chunks
