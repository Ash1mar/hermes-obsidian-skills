"""P2 file-backed normalized artifacts, source-unit generation and exact reads."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .validation import ContractError, canonical_json, fingerprint, validate_record, validate_references
from .vault_config import CONFIG_PATH, declaration

SPLITTER_VERSION = "hermes-shared-chunk-splitter/2"
ARTIFACT_ROOT = "_system/sources/artifacts"
UNIT_ROOT = "_system/sources/units"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n|$)")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_LIST = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_NUMBERED = re.compile(r"^[ \t]*(?:第[一二三四五六七八九十百零〇\d]+[章节篇]|[一二三四五六七八九十百零〇]+、|\d+(?:\.\d+)*[.)、])[ \t]*\S")
_DIVIDER = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*(?:\n|$)")
_PAGE_MARKER = re.compile(r"^[ \t]*<!--[ \t]*source-page:[ \t]*(\d+)[ \t]*-->[ \t]*(?:\n|$)")


def _fail(code: str, message: str, path: str = "$") -> None:
    raise ContractError(code, path, message)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vault_path(vault: Path, relative: str) -> Path:
    pure = PurePosixPath(str(relative).replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        _fail("UNSAFE_PATH", f"invalid Vault-relative path: {relative}")
    path = (vault / Path(*pure.parts)).resolve()
    if not path.is_relative_to(vault):
        _fail("UNSAFE_PATH", f"path escapes Vault: {relative}")
    return path


def _child_path(parent: Path, relative: str, vault: Path) -> Path:
    pure = PurePosixPath(str(relative).replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        _fail("UNSAFE_PATH", f"invalid artifact-relative path: {relative}")
    path = (parent / Path(*pure.parts)).resolve()
    if not path.is_relative_to(parent.resolve()) or not path.is_relative_to(vault):
        _fail("UNSAFE_PATH", f"artifact path escapes its root: {relative}")
    return path


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("SOURCE_UNAVAILABLE", f"cannot read JSON {path}: {exc}")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        _fail("REVISION_CONFLICT", f"source-unit publication is locked: {path}")
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _normalize_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        _fail("UNSUPPORTED_FORMAT", "normalized Markdown must be UTF-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        _fail("SOURCE_UNAVAILABLE", "normalized document is empty")
    return text


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _line_span(text: str, start_line: int, end_line: int) -> dict[str, int]:
    offsets = _line_offsets(text)
    count = len(text.splitlines()) or 1
    if start_line < 1 or end_line < start_line or end_line > count:
        _fail("INVALID_RANGE", f"line range {start_line}-{end_line} exceeds document")
    start = offsets[start_line - 1]
    end = offsets[end_line] if end_line < len(offsets) else len(text)
    return {"start": start, "end": end}


def _markdown_outline(text: str) -> dict[str, Any]:
    lines = text.splitlines(keepends=True)
    headings: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    fenced: str | None = None
    for number, line in enumerate(lines, 1):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if fenced is None:
                fenced = marker
            elif marker == fenced:
                fenced = None
            continue
        if fenced is not None:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        level, title = len(match.group(1)), match.group(2).strip()
        while stack and int(stack[-1]["level"]) >= level:
            stack.pop()
        path = [str(item["title"]) for item in stack] + [title]
        section_id = "section-" + fingerprint({"path": path, "line": number})[:20]
        entry = {"id": section_id, "title": title, "level": level,
                 "parent": stack[-1]["id"] if stack else "section-root",
                 "path": path, "start_line": number, "end_line": len(lines),
                 "pages": [], "assets": [], "quality": "pass"}
        headings.append(entry)
        stack.append(entry)
    for index, section in enumerate(headings):
        for following in headings[index + 1:]:
            if int(following["level"]) <= int(section["level"]):
                section["end_line"] = int(following["start_line"]) - 1
                break
    return {"schema_version": "source-outline/1", "document": "document.md",
            "sections": headings}


def _heuristic_outline(text: str) -> dict[str, Any]:
    lines = text.splitlines(keepends=True)
    boundaries: list[tuple[int, str, list[int]]] = []
    for number, line in enumerate(lines, 1):
        if (_NUMBERED.match(line) and len(line.strip()) <= 120
                and (number == 1 or not lines[number - 2].strip())):
            boundaries.append((number, line.strip(), []))
    if not boundaries:
        for number, line in enumerate(lines, 1):
            page = _PAGE_MARKER.match(line)
            if page:
                boundaries.append((number, f"Page {page.group(1)}", [int(page.group(1))]))
    if not boundaries:
        for number, line in enumerate(lines, 1):
            if not _DIVIDER.match(line):
                continue
            following = number + 1
            while following <= len(lines) and not lines[following - 1].strip():
                following += 1
            if following <= len(lines):
                boundaries.append((following, f"Segment {len(boundaries) + 1}", []))
    sections = []
    for index, (start, title, pages) in enumerate(boundaries):
        end = boundaries[index + 1][0] - 1 if index + 1 < len(boundaries) else len(lines)
        sections.append({"id": "section-" + fingerprint({"heuristic": title, "line": start})[:20],
                         "title": title, "level": 1, "parent": "section-root",
                         "path": [title], "start_line": start, "end_line": end,
                         "pages": pages, "assets": [], "quality": "pass"})
    return {"schema_version": "source-outline/1", "document": "document.md", "sections": sections}


def _asset_entries(bundle: Path, manifest: Mapping[str, Any], vault: Path) -> list[tuple[dict[str, Any], Path]]:
    result: list[tuple[dict[str, Any], Path]] = []
    used: set[str] = set()
    for group in ("images", "tables"):
        values = manifest.get(group, [])
        if not isinstance(values, list):
            continue
        for index, raw in enumerate(values):
            if not isinstance(raw, Mapping):
                continue
            relative = raw.get("path") or raw.get("file")
            if not isinstance(relative, str):
                continue
            source = _child_path(bundle, relative, vault)
            if not source.is_file():
                _fail("SOURCE_UNAVAILABLE", f"declared Bundle asset is missing: {relative}")
            stored = PurePosixPath(relative).as_posix()
            if stored in used:
                _fail("DUPLICATE", f"duplicate Bundle asset path: {stored}")
            used.add(stored)
            content = source.read_bytes()
            declared = raw.get("sha256")
            if declared is not None and declared != _sha(content):
                _fail("SOURCE_CHANGED", f"Bundle asset hash changed: {relative}")
            asset_id = str(raw.get("id") or raw.get("asset_id") or f"{group[:-1]}-{index + 1}")
            if not _ID.fullmatch(asset_id):
                _fail("INVALID_SCHEMA", f"invalid asset id: {asset_id}")
            result.append(({"asset_id": asset_id, "path": stored, "sha256": _sha(content),
                            "media_type": str(raw.get("media_type") or mimetypes.guess_type(stored)[0] or "application/octet-stream"),
                            "pages": [int(p) for p in raw.get("pages", [])]}, source))
    return result


def _artifact_payload(source_sha: str, document_sha: str, outline_sha: str,
                      assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source_sha256": source_sha, "normalization": "lf-codepoint/v1",
            "document_path": "document.md", "document_sha256": document_sha,
            "outline_sha256": outline_sha, "assets": assets}


class FileSourceUnitService:
    """A standard-library P2 repository rooted at one explicit Vault."""

    def __init__(self, vault_root: str | Path):
        self.vault = Path(vault_root).expanduser().resolve()
        self.vault_manifest = _load_json(_vault_path(self.vault, "_system/vault.json"))
        self.config = _load_json(_vault_path(self.vault, CONFIG_PATH))
        validate_record("config", self.config)
        source_units = self.vault_manifest.get("source_units")
        if not isinstance(source_units, Mapping) or source_units != declaration(self.config):
            _fail("INVALID_SCHEMA", "Vault does not declare the complete P2 source-reader capability")

    @property
    def vault_id(self) -> str:
        return str(self.vault_manifest.get("vault", {}).get("id", ""))

    def _identity(self, document_id: str, version_id: str, resource_id: str) -> dict[str, str]:
        identity = {"vault_id": self.vault_id, "document_id": document_id,
                    "version_id": version_id, "resource_id": resource_id}
        if not all(_ID.fullmatch(value) for value in identity.values()):
            _fail("INVALID_SCHEMA", "invalid source identity")
        return identity

    def _publish_artifact(self, identity: dict[str, str], source_sha: str, text: str,
                          outline: Mapping[str, Any], assets: list[tuple[dict[str, Any], Path]]) -> dict[str, Any]:
        document_bytes = text.encode("utf-8")
        outline_bytes = _json_bytes(outline)
        records = [item[0] for item in assets]
        payload = _artifact_payload(source_sha, _sha(document_bytes), _sha(outline_bytes), records)
        revision = fingerprint(payload)
        manifest = {"contract": "hermes-normalized-artifact/v1", "identity": identity,
                    **payload, "artifact_revision": revision}
        validate_record("artifact", manifest)
        destination = _vault_path(self.vault, f"{ARTIFACT_ROOT}/{identity['resource_id']}/{revision}")
        if destination.exists():
            existing = _load_json(destination / "manifest.json")
            if existing != manifest:
                _fail("SOURCE_CHANGED", "artifact revision directory contains different metadata")
            self._verify_artifact(destination / "manifest.json", manifest)
            return {"artifact_manifest": (destination / "manifest.json").relative_to(self.vault).as_posix(),
                    "artifact": manifest, "changed": False}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{revision}.tmp-{os.getpid()}")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            (temporary / "document.md").write_bytes(document_bytes)
            (temporary / "outline.json").write_bytes(outline_bytes)
            for asset, source in assets:
                target = _child_path(temporary, asset["path"], temporary)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"artifact_manifest": (destination / "manifest.json").relative_to(self.vault).as_posix(),
                "artifact": manifest, "changed": True}

    def prepare_markdown(self, source_path: str, document_id: str, version_id: str,
                         resource_id: str) -> dict[str, Any]:
        source = _vault_path(self.vault, source_path)
        data = source.read_bytes()
        text = _normalize_text(data)
        return self._publish_artifact(self._identity(document_id, version_id, resource_id),
                                      _sha(data), text, _markdown_outline(text), [])

    def prepare_bundle(self, bundle_path: str) -> dict[str, Any]:
        bundle = _vault_path(self.vault, bundle_path)
        manifest = _load_json(bundle / "manifest.json")
        if not str(manifest.get("schema_version", "")).startswith("2"):
            _fail("UNSUPPORTED_FORMAT", "P2 Bundle adapter requires Bundle v2")
        governance = manifest.get("governance")
        if not isinstance(governance, Mapping):
            _fail("SOURCE_UNAVAILABLE", "Bundle must carry governance identity from ingest-finish")
        identity = self._identity(str(governance.get("document_id", "")),
                                  str(governance.get("version_id", "")),
                                  str(governance.get("resource_id", "")))
        if governance.get("vault_id") != self.vault_id:
            _fail("SOURCE_CHANGED", "Bundle belongs to a different Vault")
        document_info, outline_info = manifest.get("document"), manifest.get("outline")
        if not isinstance(document_info, Mapping) or not isinstance(outline_info, Mapping):
            _fail("INVALID_SCHEMA", "Bundle must declare document and outline objects")
        document = _child_path(bundle, str(document_info.get("path", "document.md")), self.vault)
        outline_path = _child_path(bundle, str(outline_info.get("path", "outline.json")), self.vault)
        text = _normalize_text(document.read_bytes())
        outline = _load_json(outline_path)
        if not isinstance(outline, Mapping) or not isinstance(outline.get("sections"), list):
            _fail("INVALID_SCHEMA", "Bundle outline must contain sections")
        source = manifest.get("source")
        source_sha = source.get("sha256") if isinstance(source, Mapping) else None
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            _fail("INVALID_SCHEMA", "Bundle source sha256 is required")
        return self._publish_artifact(identity, source_sha, text, outline,
                                      _asset_entries(bundle, manifest, self.vault))

    def _artifact_path(self, manifest_relative: str) -> Path:
        path = _vault_path(self.vault, manifest_relative)
        expected = _vault_path(self.vault, ARTIFACT_ROOT)
        if not path.is_relative_to(expected) or path.name != "manifest.json":
            _fail("UNSAFE_PATH", "artifact manifest must be in the controlled artifact repository")
        return path

    def _verify_artifact(self, path: Path, manifest: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], str, Any]:
        value = dict(manifest or _load_json(path))
        validate_record("artifact", value)
        if value["identity"]["vault_id"] != self.vault_id:
            _fail("SOURCE_CHANGED", "artifact Vault identity mismatch")
        root = path.parent
        document = _child_path(root, value["document_path"], self.vault)
        data = document.read_bytes()
        if _sha(data) != value["document_sha256"]:
            _fail("SOURCE_CHANGED", "normalized document hash changed")
        text = _normalize_text(data)
        if text.encode("utf-8") != data:
            _fail("SOURCE_CHANGED", "normalized document is not canonical UTF-8 LF text")
        outline_path = root / "outline.json"
        outline_bytes = outline_path.read_bytes()
        if _sha(outline_bytes) != value["outline_sha256"]:
            _fail("SOURCE_CHANGED", "outline hash changed")
        outline = json.loads(outline_bytes.decode("utf-8"))
        for asset in value["assets"]:
            asset_path = _child_path(root, asset["path"], self.vault)
            if _sha(asset_path.read_bytes()) != asset["sha256"]:
                _fail("SOURCE_CHANGED", f"artifact asset hash changed: {asset['path']}")
        if fingerprint(_artifact_payload(value["source_sha256"], value["document_sha256"],
                                         value["outline_sha256"], value["assets"])) != value["artifact_revision"]:
            _fail("SOURCE_CHANGED", "artifact revision fingerprint mismatch")
        return value, text, outline

    def _sections(self, artifact: Mapping[str, Any], text: str,
                  outline: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        raw_sections = outline.get("sections")
        if not isinstance(raw_sections, list):
            _fail("INVALID_SCHEMA", "outline sections must be an array")
        line_count = len(text.splitlines()) or 1
        prepared: list[dict[str, Any]] = [{
            "id": "section-root", "parent": None, "title": "Document", "path": [],
            "level": 0, "start_line": 1, "end_line": line_count, "pages": [],
            "assets": [], "quality": "pass",
        }]
        ids = {"section-root"}
        for raw in raw_sections:
            if not isinstance(raw, Mapping):
                _fail("INVALID_SCHEMA", "outline section must be an object")
            section_id = str(raw.get("id", ""))
            if not _ID.fullmatch(section_id) or section_id in ids:
                _fail("INVALID_SCHEMA", f"invalid or duplicate section id: {section_id}")
            ids.add(section_id)
            parent = raw.get("parent") or "section-root"
            path = raw.get("path", [])
            prepared.append({
                "id": section_id, "parent": str(parent),
                "title": str(raw.get("title") or section_id),
                "path": [str(item) for item in path] if isinstance(path, list) else [],
                "level": int(raw.get("level", 1)), "start_line": int(raw["start_line"]),
                "end_line": int(raw["end_line"]),
                "pages": [int(item) for item in raw.get("pages", [])],
                "assets": [str(item) for item in raw.get("assets", [])],
                "quality": str(raw.get("quality", "pass")),
            })
        if any(item["parent"] is not None and item["parent"] not in ids for item in prepared):
            _fail("UNRESOLVED_REFERENCE", "outline contains an unknown parent section")
        by_id = {item["id"]: item for item in prepared}
        for item in prepared:
            item["scope"] = _line_span(text, item["start_line"], item["end_line"])
        children: dict[str, list[dict[str, Any]]] = {key: [] for key in by_id}
        for item in prepared[1:]:
            children[item["parent"]].append(item)
        records: list[dict[str, Any]] = []
        metadata: dict[str, dict[str, Any]] = {}
        for item in prepared:
            scope = item["scope"]
            cursor = scope["start"]
            owned: list[dict[str, int]] = []
            for child in sorted(children[item["id"]], key=lambda value: value["scope"]["start"]):
                child_scope = child["scope"]
                if child_scope["start"] < cursor or child_scope["end"] > scope["end"]:
                    _fail("INVALID_OWNERSHIP", f"child section {child['id']} overlaps or escapes {item['id']}")
                if cursor < child_scope["start"]:
                    owned.append({"start": cursor, "end": child_scope["start"]})
                cursor = child_scope["end"]
            if cursor < scope["end"]:
                owned.append({"start": cursor, "end": scope["end"]})
            record = {"contract": "hermes-source-section/v1", "section_id": item["id"],
                      "parent_id": item["parent"], "artifact_revision": artifact["artifact_revision"],
                      "path": artifact["document_path"], "title": item["title"],
                      "scope": scope, "owned_ranges": owned}
            validate_record("section", record)
            records.append(record)
            metadata[item["id"]] = item
        coverage = sorted((span["start"], span["end"]) for record in records for span in record["owned_ranges"])
        cursor = 0
        for start, end in coverage:
            if start != cursor:
                _fail("INCOMPLETE_COVERAGE", "section ownership does not partition the document")
            cursor = end
        if cursor != len(text):
            _fail("INCOMPLETE_COVERAGE", "section ownership does not cover the document")
        return records, metadata

    @staticmethod
    def _block_atoms(text: str, start: int, end: int) -> list[tuple[int, int, str | None]]:
        segment = text[start:end]
        lines = segment.splitlines(keepends=True)
        atoms: list[tuple[int, int, str | None]] = []
        offset = start
        index = 0
        while index < len(lines):
            line = lines[index]
            begin = offset
            fence = _FENCE.match(line)
            if fence:
                marker = fence.group(1)[0]
                offset += len(line); index += 1
                while index < len(lines):
                    current = lines[index]
                    offset += len(current); index += 1
                    closing = _FENCE.match(current)
                    if closing and closing.group(1)[0] == marker:
                        break
                atoms.append((begin, offset, "code")); continue
            if line.strip().startswith("$$"):
                offset += len(line); index += 1
                if line.strip().count("$$") < 2:
                    while index < len(lines):
                        current = lines[index]
                        offset += len(current); index += 1
                        if "$$" in current:
                            break
                atoms.append((begin, offset, "formula")); continue
            if "|" in line and index + 1 < len(lines) and re.match(r"^[ \t]*\|?[ :]?-{3,}", lines[index + 1]):
                offset += len(line); index += 1
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    offset += len(lines[index]); index += 1
                atoms.append((begin, offset, "table")); continue
            kind = "list" if _LIST.match(line) else None
            offset += len(line); index += 1
            while index < len(lines):
                current = lines[index]
                if not current.strip() or _FENCE.match(current) or current.strip().startswith("$$"):
                    break
                if kind == "list" and _LIST.match(current):
                    break
                if kind is None and _LIST.match(current):
                    break
                offset += len(current); index += 1
            if index < len(lines) and not lines[index].strip():
                offset += len(lines[index]); index += 1
            atoms.append((begin, offset, kind))
        if not atoms and start < end:
            atoms.append((start, end, None))
        if atoms and atoms[-1][1] != end:
            atoms.append((atoms[-1][1], end, None))
        return atoms

    @staticmethod
    def _split_normal(text: str, start: int, end: int, target: int, maximum: int,
                      separators: Iterable[str], diagnostics: list[dict[str, Any]]) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        cursor = start
        while end - cursor > maximum:
            ceiling = cursor + maximum
            preferred = min(cursor + target, ceiling)
            cut = -1
            chosen = ""
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

    def _unit_spans(self, text: str, sections: list[dict[str, Any]]) -> tuple[list[tuple[int, int, str]], list[dict[str, Any]]]:
        source = self.config["source"]
        target, maximum = source["target_codepoints"], source["max_codepoints"]
        overlap = source["overlap_codepoints"]
        merge_target = target - overlap
        diagnostics: list[dict[str, Any]] = []
        spans: list[tuple[int, int, str]] = []
        for section in sections:
            for owned in section["owned_ranges"]:
                parts: list[tuple[int, int]] = []
                protected_ranges: list[tuple[int, int]] = []
                for start, end, protected in self._block_atoms(text, owned["start"], owned["end"]):
                    if protected:
                        protected_ranges.append((start, end))
                    if protected and end - start > maximum:
                        diagnostics.append({"code": "oversized-protected-structure", "kind": protected,
                                            "section_id": section["section_id"],
                                            "span": {"start": start, "end": end},
                                            "size": end - start, "maximum": maximum})
                        parts.append((start, end))
                    elif end - start > maximum:
                        parts.extend(self._split_normal(text, start, end, merge_target, maximum,
                                                        source["separators"], diagnostics))
                    else:
                        parts.append((start, end))
                current: tuple[int, int] | None = None
                for part in parts:
                    if current is None:
                        current = part
                    elif current[1] == part[0] and part[1] - current[0] <= merge_target:
                        current = (current[0], part[1])
                    else:
                        parts_start = current[0]
                        if spans and overlap and spans[-1][2] == section["section_id"]:
                            candidate = max(owned["start"], parts_start - overlap)
                            if (current[1] - candidate <= maximum
                                    and not any(lo < parts_start and hi > candidate
                                                for lo, hi in protected_ranges)):
                                parts_start = candidate
                            else:
                                diagnostics.append({"code": "overlap-reduced", "section_id": section["section_id"],
                                                    "at": current[0], "requested": overlap,
                                                    "actual": current[0] - parts_start})
                        spans.append((parts_start, current[1], section["section_id"])); current = part
                if current is not None:
                    parts_start = current[0]
                    if spans and overlap and spans[-1][2] == section["section_id"]:
                        candidate = max(owned["start"], parts_start - overlap)
                        if (current[1] - candidate <= maximum
                                and not any(lo < parts_start and hi > candidate
                                            for lo, hi in protected_ranges)):
                            parts_start = candidate
                        else:
                            diagnostics.append({"code": "overlap-reduced", "section_id": section["section_id"],
                                                "at": current[0], "requested": overlap,
                                                "actual": current[0] - parts_start})
                    spans.append((parts_start, current[1], section["section_id"]))
        return spans, diagnostics

    def _generate(self, artifact_manifest: str, expected_revision: int) -> dict[str, Any]:
        artifact_path = self._artifact_path(artifact_manifest)
        artifact, text, outline = self._verify_artifact(artifact_path)
        self._verify_governance(artifact, None)
        source_config = self.config["source"]
        requested_strategy = source_config["strategy"]
        has_structure = bool(outline.get("sections")) if isinstance(outline, Mapping) else False
        selected_strategy = requested_strategy
        effective_outline = outline
        route_reason = "explicit configuration"
        if requested_strategy == "auto":
            selected_strategy = "structure" if has_structure else "heuristic"
            route_reason = "outline sections present" if has_structure else "no outline sections"
        if selected_strategy == "heuristic" and not has_structure:
            candidate = _heuristic_outline(text)
            if candidate["sections"]:
                effective_outline = candidate
                has_structure = True
                route_reason = "numbered, page or divider boundaries detected"
            else:
                selected_strategy = "recursive"
                route_reason = "no heuristic section boundary; recursive fallback"
        sections, metadata = self._sections(artifact, text, effective_outline)
        text_spans, split_diagnostics = self._unit_spans(text, sections)
        diagnostics = [{"code": "strategy-selected", "requested": requested_strategy,
                        "selected": selected_strategy, "reason": route_reason}, *split_diagnostics]
        unit_set_id = fingerprint({"artifact_revision": artifact["artifact_revision"],
                                   "splitter_version": SPLITTER_VERSION,
                                   "source_config": source_config})
        identity = artifact["identity"]
        base_ref = {"vault_id": identity["vault_id"], "resource_id": identity["resource_id"],
                    "artifact_revision": artifact["artifact_revision"], "unit_set_id": unit_set_id}
        assets_by_id = {asset["asset_id"]: asset for asset in artifact["assets"]}
        quality_path = f"{UNIT_ROOT}/{identity['resource_id']}/{unit_set_id}/diagnostics.json"
        drafts: list[dict[str, Any]] = []
        for start, end, section_id in text_spans:
            meta = metadata[section_id]
            linked = [asset for asset in artifact["assets"]
                      if asset["path"] in text[start:end] or asset["asset_id"] in meta["assets"]]
            locator = {"kind": "text", "path": artifact["document_path"],
                       "span": {"start": start, "end": end},
                       "line_start": text.count("\n", 0, start) + 1,
                       "line_end": text.count("\n", 0, max(start, end - 1)) + 1,
                       "pages": meta["pages"], "precision": "text-codepoint"}
            content_hash = _sha(text[start:end].encode("utf-8"))
            unit_id = fingerprint({"identity": identity, "unit_set_id": unit_set_id,
                                   "locator": locator, "content_sha256": content_hash})
            has_quality_issue = meta["quality"] != "pass" or any(
                item.get("code") == "oversized-protected-structure"
                and item.get("span", {}).get("start", end) < end
                and item.get("span", {}).get("end", start) > start
                for item in diagnostics
            )
            drafts.append({"contract": "hermes-source-unit/v1", "ref": {**base_ref, "unit_id": unit_id},
                           "document_id": identity["document_id"], "version_id": identity["version_id"],
                           "source_sha256": artifact["source_sha256"], "section_id": section_id,
                           "content_type": "normalized_source", "locator": locator,
                           "content_sha256": content_hash, "heading_path": meta["path"],
                           "asset_refs": linked, "derived_from": [],
                           "quality_refs": [quality_path] if has_quality_issue else [],
                           "prev_ref": None, "next_ref": None})
        asset_sections: dict[str, str] = {}
        for section_id, meta in metadata.items():
            for asset_id in meta["assets"]:
                if asset_id in assets_by_id and asset_id not in asset_sections:
                    asset_sections[asset_id] = section_id
        for asset in artifact["assets"]:
            locator = {"kind": "asset", "path": asset["path"], "sha256": asset["sha256"],
                       "media_type": asset["media_type"], "pages": asset["pages"],
                       "precision": "whole-asset"}
            unit_id = fingerprint({"identity": identity, "unit_set_id": unit_set_id,
                                   "locator": locator, "content_sha256": asset["sha256"]})
            section_id = asset_sections.get(asset["asset_id"])
            heading = metadata[section_id]["path"] if section_id else []
            has_quality_issue = bool(section_id and metadata[section_id]["quality"] != "pass")
            drafts.append({"contract": "hermes-source-unit/v1", "ref": {**base_ref, "unit_id": unit_id},
                           "document_id": identity["document_id"], "version_id": identity["version_id"],
                           "source_sha256": artifact["source_sha256"], "section_id": section_id,
                           "content_type": "source_asset", "locator": locator,
                           "content_sha256": asset["sha256"], "heading_path": heading,
                           "asset_refs": [], "derived_from": [],
                           "quality_refs": [quality_path] if has_quality_issue else [],
                           "prev_ref": None, "next_ref": None})
        for index, unit in enumerate(drafts):
            unit["prev_ref"] = drafts[index - 1]["ref"] if index else None
            unit["next_ref"] = drafts[index + 1]["ref"] if index + 1 < len(drafts) else None
            validate_record("unit", unit)
        current = self._current(identity["resource_id"])
        current_revision = int(current.get("revision", 0)) if current else 0
        if expected_revision != current_revision:
            _fail("REVISION_CONFLICT", f"expected source-unit revision {expected_revision}, current is {current_revision}")
        revision = current_revision if current and current.get("unit_set_id") == unit_set_id else current_revision + 1
        unit_set = {"contract": "hermes-source-unit-set/v1", "identity": identity,
                    "artifact_revision": artifact["artifact_revision"], "unit_set_id": unit_set_id,
                    "splitter_version": SPLITTER_VERSION,
                    "config_fingerprint": fingerprint(source_config),
                    "unit_ids": [unit["ref"]["unit_id"] for unit in drafts],
                    "state": "complete", "revision": revision}
        validate_references("unit_set", unit_set, drafts)
        self._verify_coverage(text, sections, drafts)
        return {"mode": "preview", "manifest": unit_set, "units": drafts,
                "sections": sections, "diagnostics": diagnostics,
                "artifact_manifest": artifact_manifest}

    @staticmethod
    def _verify_coverage(text: str, sections: list[dict[str, Any]], units: list[dict[str, Any]]) -> None:
        remaining = [(unit["section_id"], unit["locator"]["span"]["start"], unit["locator"]["span"]["end"])
                     for unit in units if unit["locator"]["kind"] == "text"]
        for section in sections:
            for owned in section["owned_ranges"]:
                selected = sorted((start, end) for section_id, start, end in remaining
                                  if section_id == section["section_id"]
                                  and start >= owned["start"] and end <= owned["end"])
                if not selected:
                    _fail("INCOMPLETE_COVERAGE", "an owned source range has no canonical chunk")
                cursor = owned["start"]
                for start, end in selected:
                    if start > cursor or end <= start:
                        _fail("INCOMPLETE_COVERAGE", "canonical chunks leave a source gap")
                    cursor = max(cursor, end)
                    remaining.remove((section["section_id"], start, end))
                if cursor != owned["end"]:
                    _fail("INCOMPLETE_COVERAGE", "canonical chunks do not cover an owned source range")
        if remaining:
            _fail("INVALID_OWNERSHIP", "a canonical chunk crosses or escapes its owned source range")

    def preview(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_build_request(request)
        result = self._generate(str(request["artifact_manifest"]), int(request["expected_revision"]))
        return result

    def build(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_build_request(request)
        generated = self._generate(str(request["artifact_manifest"]), int(request["expected_revision"]))
        manifest = generated["manifest"]
        resource = manifest["identity"]["resource_id"]
        root = _vault_path(self.vault, f"{UNIT_ROOT}/{resource}")
        destination = root / manifest["unit_set_id"]
        with _exclusive_lock(root / ".publish.lock"):
            # Recheck after acquiring the lock.
            current = self._current(resource)
            current_revision = int(current.get("revision", 0)) if current else 0
            if int(request["expected_revision"]) != current_revision:
                _fail("REVISION_CONFLICT", f"expected source-unit revision {request['expected_revision']}, current is {current_revision}")
            if destination.exists():
                existing = _load_json(destination / "manifest.json")
                if existing != manifest:
                    _fail("REVISION_CONFLICT", "an historical immutable unit set cannot be republished as current")
                # A crash can leave a fully written immutable directory before
                # current.json is replaced. Exact manifest equality distinguishes
                # that orphan from an older historical set with the same content ID.
                self.validate(resource, manifest["unit_set_id"])
            else:
                temporary = root / f".{manifest['unit_set_id']}.tmp-{os.getpid()}"
                temporary.mkdir(parents=True, exist_ok=False)
                try:
                    (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
                    (temporary / "sections.json").write_bytes(_json_bytes(generated["sections"]))
                    (temporary / "diagnostics.json").write_bytes(_json_bytes(generated["diagnostics"]))
                    lines = b"".join(canonical_json(unit) + b"\n" for unit in generated["units"])
                    (temporary / "units.jsonl").write_bytes(lines)
                    root.mkdir(parents=True, exist_ok=True)
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            pointer = {"contract": "hermes-source-unit-current/v1", "resource_id": resource,
                       "artifact_revision": manifest["artifact_revision"],
                       "unit_set_id": manifest["unit_set_id"], "revision": manifest["revision"]}
            _write_atomic(root / "current.json", _json_bytes(pointer))
        generated["mode"] = "published"
        generated["repository_path"] = destination.relative_to(self.vault).as_posix()
        return generated

    def _validate_build_request(self, request: Mapping[str, Any]) -> None:
        if request.get("config") != self.config:
            _fail("INVALID_SCHEMA", "build request must carry the exact effective Vault configuration")
        if not isinstance(request.get("actor"), str) or not request["actor"].strip():
            _fail("INVALID_SCHEMA", "build request actor is required")
        if type(request.get("expected_revision")) is not int or request["expected_revision"] < 0:
            _fail("INVALID_SCHEMA", "expected source-unit revision must be a non-negative integer")

    def _current(self, resource_id: str) -> dict[str, Any] | None:
        path = _vault_path(self.vault, f"{UNIT_ROOT}/{resource_id}/current.json")
        if not path.is_file():
            return None
        value = _load_json(path)
        expected_keys = {"contract", "resource_id", "artifact_revision", "unit_set_id", "revision"}
        if (not isinstance(value, dict) or set(value) != expected_keys
                or value.get("contract") != "hermes-source-unit-current/v1"
                or value.get("resource_id") != resource_id
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("artifact_revision", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("unit_set_id", "")))
                or type(value.get("revision")) is not int or value["revision"] < 1):
            _fail("INVALID_SCHEMA", f"invalid source-unit current pointer: {path}")
        return value

    def _load_set(self, resource_id: str, unit_set_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        selected = unit_set_id
        current = None
        if selected is None:
            current = self._current(resource_id)
            if not current:
                _fail("SOURCE_UNAVAILABLE", f"no current unit set for {resource_id}")
            selected = str(current["unit_set_id"])
        if not re.fullmatch(r"[0-9a-f]{64}", selected):
            _fail("INVALID_SCHEMA", "invalid unit-set id")
        root = _vault_path(self.vault, f"{UNIT_ROOT}/{resource_id}/{selected}")
        manifest = _load_json(root / "manifest.json")
        if (manifest.get("unit_set_id") != selected
                or manifest.get("identity", {}).get("resource_id") != resource_id):
            _fail("UNIT_SET_MISMATCH", "unit-set repository path and manifest identity disagree")
        if current and (current.get("artifact_revision") != manifest.get("artifact_revision")
                        or current.get("revision") != manifest.get("revision")):
            _fail("SOURCE_CHANGED", "current pointer and immutable unit-set manifest disagree")
        sections = _load_json(root / "sections.json")
        try:
            units = [json.loads(line) for line in (root / "units.jsonl").read_text(encoding="utf-8").splitlines() if line]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail("SOURCE_UNAVAILABLE", f"cannot read unit records: {exc}")
        return manifest, units, sections

    def validate(self, resource_id: str, unit_set_id: str | None = None) -> dict[str, Any]:
        manifest, units, sections = self._load_set(resource_id, unit_set_id)
        validate_references("unit_set", manifest, units)
        repository = _vault_path(self.vault, f"{UNIT_ROOT}/{resource_id}/{manifest['unit_set_id']}")
        diagnostics = _load_json(repository / "diagnostics.json")
        if not isinstance(diagnostics, list):
            _fail("INVALID_SCHEMA", "source-unit diagnostics must be an array")
        expected_quality_ref = (repository / "diagnostics.json").relative_to(self.vault).as_posix()
        for section in sections:
            validate_record("section", section)
            if section["artifact_revision"] != manifest["artifact_revision"]:
                _fail("UNIT_SET_MISMATCH", "section belongs to a different artifact")
        artifact_path = _vault_path(self.vault, f"{ARTIFACT_ROOT}/{resource_id}/{manifest['artifact_revision']}/manifest.json")
        artifact, text, _ = self._verify_artifact(artifact_path)
        if artifact["identity"] != manifest["identity"]:
            _fail("UNIT_SET_MISMATCH", "artifact identity differs from unit set")
        for unit in units:
            if any(reference != expected_quality_ref for reference in unit["quality_refs"]):
                _fail("UNRESOLVED_REFERENCE", "unit quality reference does not resolve to its diagnostics")
            locator = unit["locator"]
            if locator["kind"] == "text":
                content = text[locator["span"]["start"]:locator["span"]["end"]].encode("utf-8")
            else:
                content = _child_path(artifact_path.parent, locator["path"], self.vault).read_bytes()
            if _sha(content) != unit["content_sha256"]:
                _fail("SOURCE_CHANGED", f"unit content changed: {unit['ref']['unit_id']}")
        self._verify_coverage(text, sections, units)
        return {"ok": True, "resource_id": resource_id, "unit_set_id": manifest["unit_set_id"],
                "artifact_revision": manifest["artifact_revision"], "revision": manifest["revision"],
                "unit_count": len(units), "section_count": len(sections)}

    def list(self, resource_id: str, unit_set_id: str | None = None) -> list[dict[str, Any]]:
        manifest, units, _ = self._load_set(resource_id, unit_set_id)
        validate_references("unit_set", manifest, units)
        return units

    def _verify_governance(self, artifact: Mapping[str, Any], access: Mapping[str, Any] | None) -> int:
        governance = self.vault_manifest.get("governance")
        if not isinstance(governance, Mapping):
            _fail("ACCESS_DENIED", "Vault has no governance repository")
        registry_path = str(governance.get("repository", {}).get("registry_path", ""))
        registry = _load_json(_vault_path(self.vault, registry_path))
        revision = registry.get("registry_revision")
        if type(revision) is not int:
            _fail("INVALID_SCHEMA", "document registry revision is invalid")
        if access is not None and int(access.get("registry_revision", -1)) != revision:
            _fail("REVISION_CONFLICT", f"requested registry revision {access.get('registry_revision')} is not current {revision}")
        identity = artifact["identity"]
        record = next((item for item in registry.get("records", []) if isinstance(item, Mapping)
                       and item.get("version_id") == identity["version_id"]), None)
        if record is None:
            _fail("SOURCE_UNAVAILABLE", f"version is not registered: {identity['version_id']}")
        expected = {"document_id": identity["document_id"], "resource_id": identity["resource_id"],
                    "content_sha256": artifact["source_sha256"]}
        if any(record.get(key) != value for key, value in expected.items()):
            _fail("SOURCE_CHANGED", "artifact identity/hash differs from document registry")
        if record.get("processing_status") != "completed":
            _fail("SOURCE_UNAVAILABLE", "source processing is not completed")
        if access is not None and access.get("purpose") == "query":
            if record.get("governance_status") != "active":
                _fail("ACCESS_DENIED", "query reads require an active source version")
            organizations_path = str(governance.get("organizations_path", ""))
            organizations = _load_json(_vault_path(self.vault, organizations_path))
            approved = {item.get("id") for item in organizations.get("organizations", [])
                        if isinstance(item, Mapping) and item.get("status") == "approved"}
            occurrences = record.get("source_occurrences", [])
            if not occurrences or any(item.get("source_organization_id") not in approved for item in occurrences):
                _fail("ACCESS_DENIED", "query reads require approved source organizations")
        return revision

    @staticmethod
    def _find_unit(units: list[dict[str, Any]], unit_ref: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        for index, unit in enumerate(units):
            if unit["ref"] == unit_ref:
                return index, unit
        _fail("UNRESOLVED_REFERENCE", "exact full unit_ref is not present")

    def get(self, request: Mapping[str, Any]) -> dict[str, Any]:
        access = request.get("access")
        if (not isinstance(access, Mapping)
                or not isinstance(access.get("actor"), str) or not access["actor"].strip()
                or access.get("purpose") not in ("construction", "query", "qa")
                or type(access.get("registry_revision")) is not int):
            _fail("INVALID_SCHEMA", "a complete source-read access context is required")
        source_ref = request["source_ref"]
        unit_ref = source_ref["unit_ref"]
        if unit_ref.get("vault_id") != self.vault_id:
            _fail("ACCESS_DENIED", "unit reference belongs to a different Vault")
        manifest, units, _ = self._load_set(str(unit_ref["resource_id"]), str(unit_ref["unit_set_id"]))
        validate_references("unit_set", manifest, units)
        _, unit = self._find_unit(units, unit_ref)
        artifact_path = _vault_path(self.vault, f"{ARTIFACT_ROOT}/{unit_ref['resource_id']}/{unit_ref['artifact_revision']}/manifest.json")
        artifact, text, _ = self._verify_artifact(artifact_path)
        registry_revision = self._verify_governance(artifact, access)
        locator = unit["locator"]
        selected = source_ref.get("span")
        if locator["kind"] == "asset":
            if selected is not None:
                _fail("INVALID_RANGE", "whole-asset reference cannot have a text span")
            data = _child_path(artifact_path.parent, locator["path"], self.vault).read_bytes()
            if _sha(data) != unit["content_sha256"]:
                _fail("SOURCE_CHANGED", "asset content no longer matches its unit")
            core_text = data.decode("utf-8") if locator["media_type"].startswith("text/") else None
        else:
            core = locator["span"]
            span = selected or core
            if span["start"] < core["start"] or span["end"] > core["end"] or span["start"] >= span["end"]:
                _fail("OUTSIDE_UNIT", "requested span exceeds source unit core")
            full = text[core["start"]:core["end"]]
            if _sha(full.encode("utf-8")) != unit["content_sha256"]:
                _fail("SOURCE_CHANGED", "normalized text no longer matches its unit")
            core_text = text[span["start"]:span["end"]]
        return {"source_ref": source_ref, "content_sha256": unit["content_sha256"],
                "core_text": core_text, "asset_refs": unit["asset_refs"],
                "quality_refs": unit["quality_refs"], "registry_revision": registry_revision,
                "section_id": unit["section_id"], "heading_path": unit["heading_path"],
                "locator": locator}

    def context(self, request: Mapping[str, Any]) -> dict[str, Any]:
        core_refs = list(request["core_refs"])
        maximum = int(request["max_codepoints"])
        if maximum < 1:
            _fail("INVALID_BUDGET", "context maximum must be positive")
        core_results = [self.get({"source_ref": ref, "access": request["access"]}) for ref in core_refs]
        cost = sum(len(item["core_text"] or "") for item in core_results)
        if cost > maximum:
            _fail("INVALID_BUDGET", "core content alone exceeds context budget")
        seen = {canonical_json(ref["unit_ref"]) for ref in core_refs}
        candidates: list[dict[str, Any]] = []
        for ref in core_refs:
            unit_ref = ref["unit_ref"]
            _, units, sections = self._load_set(unit_ref["resource_id"], unit_ref["unit_set_id"])
            index, unit = self._find_unit(units, unit_ref)
            section_id = unit["section_id"]
            parents = {section["section_id"]: section["parent_id"] for section in sections}
            permitted = {section_id}
            cursor = section_id
            while cursor in parents and parents[cursor] is not None:
                cursor = parents[cursor]
                if cursor in permitted:
                    _fail("INVALID_OWNERSHIP", "section ancestry contains a cycle")
                permitted.add(cursor)
            ordered = sorted(range(len(units)), key=lambda i: (abs(i - index), i))
            for position in ordered:
                candidate = units[position]
                key = canonical_json(candidate["ref"])
                if key in seen or candidate["section_id"] not in permitted:
                    continue
                seen.add(key)
                candidates.append({"unit_ref": candidate["ref"], "span": None})
        context_results: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        for ref in candidates:
            result = self.get({"source_ref": ref, "access": request["access"]})
            item_cost = len(result["core_text"] or "")
            if cost + item_cost <= maximum:
                context_results.append(result); cost += item_cost
            else:
                omitted.append(ref)
        return {"core": core_results, "context": context_results, "omitted_refs": omitted,
                "truncated": bool(omitted),
                "reason": "context budget omitted section or ancestor units" if omitted else ""}
