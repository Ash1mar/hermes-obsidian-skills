"""Deterministic recursive splitting and structure-aware chunk assembly."""
from __future__ import annotations

from typing import Any, Iterable

from .atoms import Atom, atomize
from .models import CanonicalChunkSpan, ChunkProfile, TokenCounter


def split_normal(text: str, start: int, end: int, target: int, maximum: int,
                 separators: Iterable[str], diagnostics: list[dict[str, Any]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > maximum:
        ceiling, preferred, cut, chosen = cursor + maximum, min(cursor + target, cursor + maximum), -1, ""
        for separator in separators:
            position = text.rfind(separator, cursor + 1, ceiling + 1)
            if position >= cursor + max(1, target // 3):
                candidate = position + len(separator)
                if cut < 0 or abs(candidate - preferred) < abs(cut - preferred):
                    cut, chosen = candidate, separator
        if cut <= cursor:
            cut = ceiling
            diagnostics.append({"code": "forced-split", "span": {"start": cursor, "end": cut},
                                "reason": "no configured semantic separator before maximum"})
        else:
            diagnostics.append({"code": "recursive-split", "span": {"start": cursor, "end": cut},
                                "separator": chosen})
        result.append((cursor, cut)); cursor = cut
    if cursor < end:
        result.append((cursor, end))
    return result


def _token_split(text: str, start: int, end: int, counter: TokenCounter, maximum: int,
                 separators: Iterable[str], diagnostics: list[dict[str, Any]]) -> list[tuple[int, int]]:
    if counter.count(text[start:end]) <= maximum:
        return [(start, end)]
    result, cursor = [], start
    while cursor < end:
        low, high, best = cursor + 1, end, cursor
        while low <= high:
            middle = (low + high) // 2
            if counter.count(text[cursor:middle]) <= maximum:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if best <= cursor:
            best = cursor + 1
        cut = best
        floor = cursor + max(1, (best - cursor) // 3)
        for separator in separators:
            position = text.rfind(separator, floor, best + 1)
            if position >= floor:
                cut = position + len(separator)
                break
        diagnostics.append({"code": "token-hard-split", "span": {"start": cursor, "end": cut},
                            "tokenizer_fingerprint": counter.fingerprint})
        result.append((cursor, cut)); cursor = cut
    return result


def build_base_spans(text: str, sections: list[dict[str, Any]], profile: ChunkProfile,
                     token_counter: TokenCounter | None) -> tuple[list[CanonicalChunkSpan], list[dict[str, Any]], list[tuple[int, int]]]:
    diagnostics: list[dict[str, Any]] = []
    spans: list[CanonicalChunkSpan] = []
    protected_ranges: list[tuple[int, int]] = []
    merge_target = profile.target_codepoints - profile.overlap_codepoints
    for section in sections:
        for owned in section["owned_ranges"]:
            parts: list[tuple[int, int, tuple[str, ...]]] = []
            for atom in atomize(text, owned["start"], owned["end"]):
                kinds = (atom.kind,) if atom.kind else ()
                if atom.kind:
                    protected_ranges.append((atom.start, atom.end))
                oversized_codepoints = atom.end - atom.start > profile.max_codepoints
                oversized_tokens = bool(token_counter and profile.token_mode == "hard"
                                        and token_counter.count(text[atom.start:atom.end]) > profile.max_tokens)
                if atom.kind and (oversized_codepoints or oversized_tokens):
                    diagnostics.append({
                        "code": "oversized-protected-structure", "kind": atom.kind,
                        "section_id": section["section_id"], "span": {"start": atom.start, "end": atom.end},
                        "size": atom.end - atom.start, "maximum": profile.max_codepoints,
                        "tokens": token_counter.count(text[atom.start:atom.end]) if token_counter else None,
                    })
                    parts.append((atom.start, atom.end, kinds))
                    continue
                raw = (split_normal(text, atom.start, atom.end, merge_target, profile.max_codepoints,
                                    profile.separators, diagnostics)
                       if oversized_codepoints else [(atom.start, atom.end)])
                if token_counter and profile.token_mode == "hard":
                    raw = [piece for begin, finish in raw
                           for piece in _token_split(text, begin, finish, token_counter,
                                                     profile.max_tokens, profile.separators, diagnostics)]
                parts.extend((begin, finish, kinds) for begin, finish in raw)
            current: tuple[int, int, tuple[str, ...]] | None = None
            for part in parts:
                if current is None:
                    current = part
                elif (current[1] == part[0] and part[1] - current[0] <= merge_target
                      and not (current[2] or part[2])
                      and not (token_counter and profile.token_mode == "hard"
                               and token_counter.count(text[current[0]:part[1]]) > profile.max_tokens)):
                    current = (current[0], part[1], ())
                else:
                    spans.append(CanonicalChunkSpan(section["section_id"], *current[:2], current[2]))
                    current = part
            if current is not None:
                spans.append(CanonicalChunkSpan(section["section_id"], *current[:2], current[2]))
    return spans, diagnostics, protected_ranges
