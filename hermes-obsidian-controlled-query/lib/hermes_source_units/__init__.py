"""Provider-independent SourceUnit, knowledge-build and Vault Finalize runtime."""

from .chunk_engine import (ENGINE_FINGERPRINT, ENGINE_VERSION, ChunkEngineInput,
                           ChunkProfile, SharedChunkEngine, TokenCounter,
                           UnicodeCodepointCounter)
from .knowledge_build import FileKnowledgeBuildService
from .ingest_workflow import FileIngestWorkflowService, mutation_digest
from .source_units import FileSourceUnitService
from .vault_finalize import FileVaultFinalizeService
from .validation import ContractError, validate_record, validate_references

__version__ = "0.4.0"
__all__ = ["ChunkEngineInput", "ChunkProfile", "ContractError", "ENGINE_FINGERPRINT",
           "ENGINE_VERSION", "FileIngestWorkflowService", "FileKnowledgeBuildService", "FileSourceUnitService", "FileVaultFinalizeService", "SharedChunkEngine", "mutation_digest",
           "TokenCounter", "UnicodeCodepointCounter", "validate_record", "validate_references"]
