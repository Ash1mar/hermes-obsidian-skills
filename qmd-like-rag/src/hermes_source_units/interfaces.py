"""Structural interfaces implemented by the P2-P4 file-backed services.

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
    current access policy. FileSourceUnitService implements this protocol.
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


class KnowledgeBuildRepository(Protocol):
    """P3 task, Pass/Reduce and Build Finalize boundary.

    Semantic candidate and identity judgments are explicit inputs. Implementations
    verify exact UnitRefs, revisions, reviews and repository state; they do not use
    fuzzy name matching to decide identity.
    """

    def plan_task(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def measure_reading_window(self, unit_refs: list[Mapping[str, Any]],
                               registry_revision: int,
                               unitset_revisions: list[Mapping[str, Any]] | None,
                               reader_config: Mapping[str, Any], *,
                               actor: str) -> Mapping[str, Any]: ...
    def measure_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def plan_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def adopt_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def batch_next_slice(self, batch_id: str, worker_id: str,
                         config: Mapping[str, Any] | None = None,
                         lease_seconds: int | None = None,
                         now: Any | None = None) -> Mapping[str, Any]: ...
    def slice_heartbeat(self, batch_id: str, slice_id: str, worker_id: str,
                        expected_revision: int, now: Any | None = None) -> Mapping[str, Any]: ...
    def slice_complete(self, request: Mapping[str, Any],
                       now: Any | None = None) -> Mapping[str, Any]: ...
    def slice_fail(self, request: Mapping[str, Any], now: Any | None = None) -> Mapping[str, Any]: ...
    def reconcile_slice_inputs(self, batch_id: str, slice_id: str, actor: str,
                               expected_revision: int) -> Mapping[str, Any]: ...
    def reclaim_expired_slices(self, batch_id: str,
                               now: Any | None = None) -> Mapping[str, Any]: ...
    def cancel_batch(self, batch_id: str, actor: str,
                     expected_revision: int) -> Mapping[str, Any]: ...
    def batch_status(self, batch_id: str, compact: bool = False) -> Mapping[str, Any]: ...
    def resume_batch(self, batch_id: str) -> Mapping[str, Any]: ...
    def set_task_state_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def claim_task(self, task_id: str, actor: str, expected_revision: int) -> Mapping[str, Any]: ...
    def reading_material(self, task_id: str, actor: str, registry_revision: int,
                         max_codepoints: int | None = None) -> Mapping[str, Any]: ...
    def prepare_batch(self, batch_id: str, actor: str, registry_revision: int,
                      max_codepoints: int | None = None) -> Mapping[str, Any]: ...
    def record_pass(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def record_pass_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def reduce(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def reduce_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def finalize(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def finalize_batch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def validate_run(self, run_id: str) -> Mapping[str, Any]: ...
    def validate_batch(self, batch_id: str) -> Mapping[str, Any]: ...


class VaultFinalizeRepository(Protocol):
    """P4 incremental Vault release boundary."""

    def plan(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def apply(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def validate(self, release_id: str) -> Mapping[str, Any]: ...

    def status(self) -> Mapping[str, Any]: ...
