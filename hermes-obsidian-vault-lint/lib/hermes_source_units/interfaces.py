"""Structural interfaces for P2 implementations. These are not working services.

All spans use LF-normalized Unicode code points, zero-based [start, end).
Reference spans are absolute within the referenced unit's source file.
"""

from typing import Any, Literal, Mapping, Protocol, TypedDict


class Span(TypedDict):
    start: int
    end: int


class UnitRef(TypedDict):
    vault_id: str
    resource_id: str
    artifact_revision: str
    unit_set_id: str
    unit_id: str


class SourceRef(TypedDict):
    unit_ref: UnitRef
    span: Span | None  # null means the whole unit, including whole-asset units


class AccessContext(TypedDict):
    actor: str
    purpose: Literal["construction", "query", "qa"]
    registry_revision: int


class BuildRequest(TypedDict):
    artifact_manifest: str  # Vault-relative, runtime resolves under explicit vault_root
    config: Mapping[str, Any]
    actor: str
    expected_revision: int


class BuildResult(TypedDict):
    mode: Literal["preview", "published"]
    manifest: Mapping[str, Any]
    units: list[Mapping[str, Any]]
    diagnostics: list[Mapping[str, Any]]


class ReadRequest(TypedDict):
    source_ref: SourceRef
    access: AccessContext


class ReadResult(TypedDict):
    source_ref: SourceRef
    content_sha256: str
    core_text: str | None  # assets can have no text; never invent OCR
    asset_refs: list[Mapping[str, Any]]
    quality_refs: list[str]
    registry_revision: int


class ContextRequest(TypedDict):
    core_refs: list[SourceRef]
    access: AccessContext
    max_codepoints: int


class ContextResult(TypedDict):
    core: list[ReadResult]
    context: list[ReadResult]
    omitted_refs: list[SourceRef]
    truncated: bool
    reason: str


class SourceUnitReader(Protocol):
    """Must verify hashes and current governance, never rebuild on read.

    Construct with an explicit vault_root. Implementations must resolve symlinks
    under that root, reject inaccessible/changed versions and report errors using
    ContractError codes documented in README. Historical reads still check the
    current access policy. No implementation ships in P0.
    """

    def get(self, request: ReadRequest) -> ReadResult: ...
    def context(self, request: ContextRequest) -> ContextResult: ...


class SourceUnitBuilder(Protocol):
    """Preview and build use identical generation, normalization and diagnostics.

    Preview does not write authoritative records. Build publishes only a complete,
    verified manifest under revision control. Model budgets belong to consumers.
    """

    def preview(self, request: BuildRequest) -> BuildResult: ...
    def build(self, request: BuildRequest) -> BuildResult: ...
