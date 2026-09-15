"""Lightweight token-counter interfaces; model adapters are supplied by consumers."""


class UnicodeCodepointCounter:
    """Explicit diagnostic counter for tests and CLI smoke audits, not a model tokenizer."""

    @property
    def fingerprint(self) -> str:
        return "hermes-token-counter/unicode-codepoints-v1"

    def count(self, text: str) -> int:
        return len(text)
