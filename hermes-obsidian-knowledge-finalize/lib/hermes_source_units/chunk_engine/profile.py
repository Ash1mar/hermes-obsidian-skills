"""Document structure signals and deterministic heuristic outline generation."""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..validation import fingerprint
from .atoms import FENCE

NUMBERED = re.compile(r"^[ \t]*(?:第[一二三四五六七八九十百零〇\d]+[章节篇]|[一二三四五六七八九十百零〇]+、|\d+(?:\.\d+)*[.)、])[ \t]*\S")
DIVIDER = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
PAGE = re.compile(r"^[ \t]*<!--[ \t]*source-page:[ \t]*(\d+)[ \t]*-->[ \t]*$")
SENTENCE_END = re.compile(r"[。！？；.!?;:]$")


def document_profile(text: str, outline: Mapping[str, Any]) -> dict[str, Any]:
    lines = text.splitlines()
    outline_sections = outline.get("sections", [])
    numbered = sum(bool(NUMBERED.match(line)) for line in lines)
    pages = sum(bool(PAGE.match(line)) for line in lines)
    dividers = sum(bool(DIVIDER.match(line)) for line in lines)
    visible = [line for line in lines if line.strip()]
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return {
        "codepoints": len(text),
        "lines": len(lines) or 1,
        "nonempty_lines": len(visible),
        "outline_sections": len(outline_sections) if isinstance(outline_sections, list) else 0,
        "numbered_boundaries": numbered,
        "page_markers": pages,
        "divider_boundaries": dividers,
        "language_signal": "mixed" if cjk and latin else "cjk" if cjk else "latin" if latin else "unknown",
    }


def heuristic_outline(text: str) -> dict[str, Any]:
    lines = text.splitlines(keepends=True)
    eligible: list[bool] = []
    fenced: str | None = None
    for line in lines:
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)[0]
            eligible.append(False)
            fenced = token if fenced is None else None if fenced == token else fenced
        else:
            eligible.append(fenced is None)
    boundaries: list[tuple[int, str, list[int], str]] = []
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if (eligible[number - 1] and NUMBERED.match(line) and len(stripped) <= 120
                and (number == 1 or not lines[number - 2].strip())):
            boundaries.append((number, stripped, [], "numbered"))
    if not boundaries:
        for number, line in enumerate(lines, 1):
            page = PAGE.match(line.strip("\n"))
            if eligible[number - 1] and page:
                boundaries.append((number, f"Page {page.group(1)}", [int(page.group(1))], "page"))
    if not boundaries:
        for number, line in enumerate(lines, 1):
            if eligible[number - 1] and DIVIDER.match(line.strip("\n")):
                following = number + 1
                while following <= len(lines) and not lines[following - 1].strip():
                    following += 1
                if following <= len(lines):
                    boundaries.append((following, lines[following - 1].strip() or f"Segment {len(boundaries) + 1}", [], "divider"))
    if not boundaries:
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            before_blank = number == 1 or not lines[number - 2].strip()
            after_blank = number == len(lines) or not lines[number].strip()
            if (eligible[number - 1] and 2 <= len(stripped) <= 50 and before_blank and after_blank
                    and not SENTENCE_END.search(stripped)):
                boundaries.append((number, stripped, [], "short-title"))
    sections = []
    for index, (start, title, pages, signal) in enumerate(boundaries):
        end = boundaries[index + 1][0] - 1 if index + 1 < len(boundaries) else len(lines)
        sections.append({
            "id": "section-" + fingerprint({"heuristic": signal, "title": title, "line": start})[:20],
            "title": title, "level": 1, "parent": "section-root", "path": [title],
            "start_line": start, "end_line": end, "pages": pages, "assets": [],
            "quality": "pass", "heuristic_signal": signal,
        })
    return {"schema_version": "source-outline/2", "document": "document.md", "sections": sections}
