"""P0 contracts only; no splitter, source reader, ledger or Provider runtime."""

from .validation import ContractError, validate_record, validate_references

__version__ = "0.1.1"
__all__ = ["ContractError", "validate_record", "validate_references"]
