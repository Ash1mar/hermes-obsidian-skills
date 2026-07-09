---
name: hermes-obsidian-vault-lint
description: Read-only governance linting for Hermes + Obsidian vaults. Use when asked to check vault health, validate post-ingest acceptance, audit Bundle v2 and section-ledger state, verify source-map alignment, inspect artifact frontmatter and citation contracts, find stale or in-progress ingestion state, review QA boundaries, or decide whether a governed vault is query-ready without ingesting new sources or writing durable artifacts by default.
---

# Hermes Obsidian Vault Lint

Audit a governed Hermes + Obsidian vault without changing it.

```text
prompt scope
-> use configured intranet vault path
-> choose lint profile
-> run read-only lint script
-> report errors, warnings, metrics, and next actions
```

## Intranet Vault Configuration

On the `intranet` branch, the lint target vault path is fixed by this skill's config:

- config file: `config/intranet.json`
- configured vault path: `/opt/data/phq/testVault`

Use this configured path by default. If the server vault path changes, update `config/intranet.json`; do not switch vaults by prompt wording.

## Boundary

- Do not ask the user for a vault path on this branch. Use the configured intranet path.
- Treat lint as read-only. Do not create, edit, move, rename, or delete vault files unless the user explicitly asks for a report file or a later fix operation.
- Do not run controlled ingest or controlled query as part of lint. Lint may point to those workflows as follow-up actions.
- Do not treat `qa_required` as a failure by default. It is a controlled open QA state unless the selected profile says otherwise.
- Do not promote formulas, table values, figure internals, or engineering parameters from warning-affected evidence.

## Quick Start

Run:

```bash
python hermes-obsidian-vault-lint/scripts/lint_vault.py \
  --profile post-ingest \
  --json
```

Use `--markdown-report <path>` only when the user explicitly asks to persist a lint report.

If the script cannot locate the controlled-ingest skill automatically, pass:

```bash
--ingest-skill-path "/path/to/hermes-obsidian-controlled-ingest"
```

## Profiles

| Profile | Use for | Default stance |
| --- | --- | --- |
| `post-ingest` | after controlled ingest or batch ingest | allow `qa_required`; error on broken structure, failed bundles, stale or in-progress ledger state |
| `query-ready` | before answering from a vault | emphasize citation contract and evidence traceability |
| `strict` | publication, archive, or handoff | escalate open QA and weak traceability |
| `qa-review` | planning targeted QA work | emphasize QA sections and warning-affected evidence |

Read `references/profiles.md` before changing profile semantics.

## Script Output

The script emits a stable JSON envelope:

```json
{
  "ok": true,
  "status": "pass-with-warnings",
  "profile": "post-ingest",
  "summary": {"errors": 0, "warnings": 1, "info": 0},
  "issues": [],
  "metrics": {}
}
```

Read `references/output-contract.md` before consuming the JSON from another script or automation.

## Rule Scope

First-phase rules cover:

- vault folder and governance-file structure
- Markdown frontmatter presence and basic required fields
- Bundle v2 validation through the controlled-ingest bundle validator
- section-ledger JSON parseability, status vocabulary, and active/stale state
- source-map frontmatter alignment with ledger bundle id, revision, state, and validation status
- source citation fields for single-source governed artifacts
- ledger output paths resolving to existing governed files

Read `references/rule-catalog.md` before adding or changing rule codes.

## Reporting

When responding to the user:

1. State the vault linted and profile used.
2. Lead with `status`, error count, warning count, and major metrics.
3. List actionable errors first, then warnings.
4. Distinguish controlled QA from broken state.
5. Suggest the next controlled workflow only when it follows directly from an issue.

If no errors are found but warnings remain, call the result `pass-with-warnings`, not failure.
