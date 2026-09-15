"""Markdown-aware atoms that ordinary chunking must not split."""
from __future__ import annotations

import re
from dataclasses import dataclass

FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
LIST = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
TABLE_SEPARATOR = re.compile(r"^[ \t]*\|?[ :]?-{3,}")
ASSET = re.compile(r"(?:!\[[^\]]*\]\([^\n)]+\)|<img\b[^>]*>)", re.IGNORECASE)


@dataclass(frozen=True)
class Atom:
    start: int
    end: int
    kind: str | None


def atomize(text: str, start: int, end: int) -> list[Atom]:
    lines = text[start:end].splitlines(keepends=True)
    atoms: list[Atom] = []
    offset = start
    index = 0
    while index < len(lines):
        line, begin = lines[index], offset
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            offset += len(line); index += 1
            while index < len(lines):
                current = lines[index]
                offset += len(current); index += 1
                closing = FENCE.match(current)
                if closing and closing.group(1)[0] == marker:
                    break
            atoms.append(Atom(begin, offset, "code")); continue
        if line.strip().startswith("$$"):
            offset += len(line); index += 1
            if line.strip().count("$$") < 2:
                while index < len(lines):
                    current = lines[index]
                    offset += len(current); index += 1
                    if "$$" in current:
                        break
            atoms.append(Atom(begin, offset, "formula")); continue
        if "|" in line and index + 1 < len(lines) and TABLE_SEPARATOR.match(lines[index + 1]):
            offset += len(line); index += 1
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                offset += len(lines[index]); index += 1
            atoms.append(Atom(begin, offset, "table")); continue
        if LIST.match(line):
            offset += len(line); index += 1
            while index < len(lines):
                current = lines[index]
                if not current.strip():
                    offset += len(current); index += 1
                    break
                if FENCE.match(current) or current.strip().startswith("$$"):
                    break
                if LIST.match(current) or current.startswith((" ", "\t")):
                    offset += len(current); index += 1
                    continue
                break
            atoms.append(Atom(begin, offset, "list")); continue
        kind = "asset-reference" if ASSET.search(line) else None
        offset += len(line); index += 1
        while index < len(lines):
            current = lines[index]
            if not current.strip() or FENCE.match(current) or current.strip().startswith("$$") or LIST.match(current):
                break
            if "|" in current and index + 1 < len(lines) and TABLE_SEPARATOR.match(lines[index + 1]):
                break
            kind = kind or ("asset-reference" if ASSET.search(current) else None)
            offset += len(current); index += 1
        if index < len(lines) and not lines[index].strip():
            offset += len(lines[index]); index += 1
        atoms.append(Atom(begin, offset, kind))
    if not atoms and start < end:
        atoms.append(Atom(start, end, None))
    if atoms and atoms[-1].end != end:
        atoms.append(Atom(atoms[-1].end, end, None))
    return atoms
