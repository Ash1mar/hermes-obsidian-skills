"""Provider-independent source-unit contracts and P2 file runtime."""

from .source_units import FileSourceUnitService, SPLITTER_VERSION
from .validation import ContractError, validate_record, validate_references

__version__ = "0.2.0"
__all__ = ["ContractError", "FileSourceUnitService", "SPLITTER_VERSION",
           "validate_record", "validate_references"]
