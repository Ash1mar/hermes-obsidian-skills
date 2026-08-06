from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceDocument:
    path: Path
    vault_path: str


def is_valid_markdown(path: Path, vault_root: Path) -> bool:
    if not path.is_file() or path.suffix.casefold() != ".md":
        return False
    relative = path.resolve().relative_to(vault_root.resolve())
    return not any(part.startswith((".", "~")) for part in relative.parts)


def resolve_sources(vault_root: Path, include_patterns: list[str]) -> list[SourceDocument]:
    vault_root = vault_root.resolve()
    unique: dict[str, SourceDocument] = {}
    for pattern in include_patterns:
        for path in vault_root.glob(pattern):
            if not is_valid_markdown(path, vault_root):
                continue
            relative = path.resolve().relative_to(vault_root).as_posix()
            unique[relative] = SourceDocument(path=path.resolve(), vault_path=relative)
    return [unique[key] for key in sorted(unique)]
