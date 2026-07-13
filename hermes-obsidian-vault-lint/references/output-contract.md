# Output Contract

`lint_vault.py` prints one JSON object by default when `--json` is passed.

## Envelope

```json
{
  "tool": "hermes-obsidian-vault-lint",
  "schema_version": "1.0",
  "ok": true,
  "status": "pass|pass-with-warnings|fail|internal-error",
  "profile": "post-ingest",
  "vault": "/absolute/path/to/vault",
  "summary": {
    "errors": 0,
    "warnings": 0,
    "info": 0
  },
  "metrics": {},
  "issues": [],
  "trace": []
}
```

## Issue Fields

Each issue has:

```json
{
  "code": "ledger.in_progress",
  "severity": "error|warning|info",
  "path": "_system/reports/example.section-ledger.json",
  "message": "Human-readable finding.",
  "hint": "Actionable next step.",
  "details": {}
}
```

## Exit Codes

- `0`: pass or pass-with-warnings
- `1`: warnings when `--fail-on warning`
- `2`: errors
- `3`: internal lint failure

Consumers should rely on `status`, `summary`, and `issues[].code`, not console formatting.

## Semantic Metrics

When applicable, `metrics` includes:

- `multi_source_artifacts`
- `structured_multi_source_artifacts`
- `qa_affected_artifacts`
