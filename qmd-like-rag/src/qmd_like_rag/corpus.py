from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from hermes_source_units import FileSourceUnitService


RELEASE_STATE = "_system/metadata/knowledge-release-state.json"
RELEASE_ROOT = "_system/knowledge-releases"
PAGE_REVISIONS = "_system/knowledge-builds/page-revisions"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ValueError(f"Invalid Vault-relative path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class ReleaseCorpus:
    release_id: str
    release_hash: str
    release: dict[str, Any]
    projections: tuple[dict[str, Any], ...]


def current_release_identity(vault_root: Path) -> tuple[str, str]:
    vault_root = vault_root.resolve()
    state = _json(vault_root / RELEASE_STATE)
    release_id = state.get("current_release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("Vault has no current knowledge release")
    data = (vault_root / RELEASE_ROOT / release_id / "manifest.json").read_bytes()
    manifest = json.loads(data.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("release_id") != release_id or manifest.get("state") != "completed":
        raise ValueError("Current knowledge release pointer and manifest disagree")
    return release_id, _sha(data)


def _registry_revision(vault_root: Path, source: FileSourceUnitService) -> int:
    governance = source.vault_manifest.get("governance", {})
    path = _relative(str(governance.get("repository", {}).get("registry_path", "")))
    revision = _json(vault_root / path).get("registry_revision")
    if type(revision) is not int:
        raise ValueError("Document registry has no integer registry_revision")
    return revision


def _oversized_protected_spans(vault_root: Path, unit: dict[str, Any]) -> list[dict[str, int]]:
    locator = unit.get("locator", {})
    core = locator.get("span") if locator.get("kind") == "text" else None
    if not isinstance(core, dict):
        return []
    spans: list[dict[str, int]] = []
    for relative in unit.get("quality_refs", []):
        report = _json(vault_root / _relative(str(relative)))
        for diagnostic in report.get("diagnostics", []):
            span = diagnostic.get("span", {}) if isinstance(diagnostic, dict) else {}
            if (diagnostic.get("code") == "oversized-protected-structure"
                    and type(span.get("start")) is int and type(span.get("end")) is int
                    and span["start"] < core["end"] and span["end"] > core["start"]):
                spans.append({"start": max(core["start"], span["start"]),
                              "end": min(core["end"], span["end"])})
    return spans


def _source_projections(vault_root: Path, source: FileSourceUnitService, item: dict[str, Any], release_id: str,
                        release_hash: str, registry_revision: int) -> list[dict[str, Any]]:
    resource_id = str(item["resource_id"])
    unit_set_id = str(item["unit_set_id"])
    source.validate(resource_id, unit_set_id)
    output = []
    for unit in source.list(resource_id, unit_set_id):
        ref = dict(unit["ref"])
        read = source.get({
            "source_ref": {"unit_ref": ref, "span": None},
            "access": {"actor": "qmd-like-rag", "purpose": "query", "registry_revision": registry_revision},
        })
        locator = unit["locator"]
        heading = [str(value) for value in unit.get("heading_path", [])]
        core = read.get("core_text")
        if core is None:
            core = "\n".join(filter(None, [" / ".join(heading), f"Asset: {locator.get('path', '')}",
                                                   f"Media type: {locator.get('media_type', '')}"]))
        output.append({
            "projection_kind": "source_unit", "release_id": release_id,
            "release_hash": release_hash, "unit_ref": ref, "page_revision_id": None,
            "source_unit_refs": [], "core_text": core, "heading_path": heading,
            "vault_path": _relative(
                f"_system/sources/artifacts/{resource_id}/{ref['artifact_revision']}/{locator['path']}"),
            "line_start": int(locator.get("line_start") or 1),
            "line_end": int(locator.get("line_end") or locator.get("line_start") or 1),
            "source_sha256": str(unit["source_sha256"]),
            "content_sha256": str(unit["content_sha256"]),
            "content_type": str(unit["content_type"]),
            "quality_refs": list(unit.get("quality_refs", [])),
            "locator_span": locator.get("span"),
            "oversized_protected_spans": _oversized_protected_spans(vault_root, unit),
        })
    return output


def _page_projection(vault_root: Path, release: dict[str, Any], item: dict[str, Any],
                     release_hash: str) -> dict[str, Any]:
    page_id = str(item["id"])
    revision_id = str(item["page_revision_id"])
    subject = next((row for row in release.get("subjects", []) if row.get("page_id") == page_id), None)
    if not subject:
        raise ValueError(f"Eligible page is absent from release subjects: {page_id}")
    relative = _relative(str(subject["current_path"]))
    sidecar = _json(vault_root / PAGE_REVISIONS / page_id / f"{revision_id}.json")
    if sidecar.get("page_id") != page_id or sidecar.get("revision_id") != revision_id or sidecar.get("state") != "committed":
        raise ValueError(f"Invalid committed page revision: {page_id}/{revision_id}")
    content = (vault_root / relative).read_bytes()
    if _sha(content) != sidecar.get("authored_sha256"):
        raise ValueError(f"Knowledge page differs from committed revision: {relative}")
    return {
        "projection_kind": "knowledge_page", "release_id": str(release["release_id"]),
        "release_hash": release_hash, "unit_ref": None, "page_revision_id": revision_id,
        "page_id": page_id, "source_unit_refs": list(item.get("source_unit_refs", [])),
        "core_text": content.decode("utf-8"),
        "heading_path": [str(subject.get("current_path", page_id))], "vault_path": relative,
        "line_start": 1, "line_end": max(1, content.count(b"\n") + 1),
        "source_sha256": str(sidecar["authored_sha256"]),
        "content_sha256": str(sidecar["authored_sha256"]),
        "content_type": "knowledge_page", "quality_refs": [],
    }


def load_release_corpus(vault_root: Path) -> ReleaseCorpus:
    vault_root = vault_root.resolve()
    release_id, expected_hash = current_release_identity(vault_root)
    manifest_path = vault_root / RELEASE_ROOT / release_id / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    release = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(release, dict) or release.get("contract") != "hermes-knowledge-release/v1":
        raise ValueError("Current knowledge release manifest is invalid")
    if release.get("release_id") != release_id or release.get("state") != "completed":
        raise ValueError("Current knowledge release pointer and manifest disagree")
    release_hash = _sha(manifest_bytes)
    if release_hash != expected_hash:
        raise ValueError("Knowledge release changed while loading corpus")
    source = FileSourceUnitService(vault_root)
    registry_revision = _registry_revision(vault_root, source)
    projections: list[dict[str, Any]] = []
    for item in release.get("index_eligibility", []):
        if not item.get("eligible"):
            continue
        if item.get("kind") == "source_unit_set":
            projections.extend(_source_projections(vault_root, source, item, release_id, release_hash,
                                                   registry_revision))
        elif item.get("kind") == "knowledge_page":
            projections.append(_page_projection(vault_root, release, item, release_hash))
        else:
            raise ValueError(f"Unsupported release projection kind: {item.get('kind')!r}")
    return ReleaseCorpus(release_id, release_hash, release, tuple(projections))
