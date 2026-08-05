---
name: hermes-obsidian-vault-lint
description: Vault 只读审计 / Read-only Vault Lint：检查受治理 Obsidian Vault 健康、摄取验收、Bundle、section ledger、source map、证据链、过期状态、QA 边界或查询就绪状态时使用。On Hermes, MUST call skill_view for hermes-obsidian-vault-lint and load its complete scripts before linting; on other runtimes, load the full skill first.
---

# Hermes Obsidian Vault Lint

Audit a governed Hermes + Obsidian vault without changing it.

## Runtime Skill Boundary

Use `<lint-skill-root>` as the runtime-neutral directory containing this active `SKILL.md`, not the parent directory that contains multiple Skills. The package layout is `<lint-skill-root>/SKILL.md`, `<lint-skill-root>/scripts/*.py`, `<lint-skill-root>/references/*.md`, and optional `<lint-skill-root>/config/*.json`. Resolve it from the active runtime's loader. On Hermes, use the concrete expanded `${HERMES_SKILL_DIR}` or the `skill_dir` returned by `skill_view(name="hermes-obsidian-vault-lint")`; on another runtime, use its equivalent active-skill directory.

Resolve bundled `scripts/`, `references/`, and `config/` against `<lint-skill-root>`. Execute Python entry points as `python3 "<lint-skill-root>/scripts/<script>.py"`. Never infer a conventional installation path. On Hermes, inspect `skill_view` and `linked_files.scripts` before declaring the lint script uninstalled; a terminal sandbox path failure may instead be a missing mount. Lint remains read-only and must not repair the Vault itself.

On this branch, `/opt/data/skills` from `config/intranet.json` is the parent containing all Skills, not `<lint-skill-root>`. The expected package is `/opt/data/skills/hermes-obsidian-vault-lint/`, with the script at `/opt/data/skills/hermes-obsidian-vault-lint/scripts/lint_vault.py`. Prefer `skill_view.skill_dir`; use the configured package only as a fallback and consistency check.

```text
prompt scope
-> use configured intranet vault path
-> choose lint profile
-> run read-only lint script
-> report errors, warnings, metrics, and next actions
```

## Boundary

- Do not ask the user for a vault path on this branch. Use the configured intranet path.
- Treat lint as read-only. Do not create, edit, move, rename, or delete vault files unless the user explicitly asks for a report file or a later fix operation.
- Do not run controlled ingest or controlled query as part of lint. Lint may point to those workflows as follow-up actions.
- Do not treat `qa_required` as a failure by default. It is a controlled open QA state unless the selected profile says otherwise.
- Do not promote formulas, table values, figure internals, or engineering parameters from warning-affected evidence.

## Quick Start

Run:

```bash
python3 "<lint-skill-root>/scripts/lint_vault.py" \
  --profile post-ingest \
  --json
```

## Intranet Vault Configuration

Use `config/intranet.json` as the default lint target on this branch. It defines Vault `/opt/data/phq/testVault` and Hermes Skills parent `/opt/data/skills`.

Use `--vault` only as an explicit operational override. Do not select another vault from prompt wording alone.

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

Rules cover:

- vault folder and governance-file structure
- Markdown frontmatter presence and basic required fields
- Bundle v2 validation through the controlled-ingest bundle validator
- section-ledger JSON parseability, status vocabulary, and active/stale state
- source-map frontmatter alignment with ledger bundle id, revision, state, and validation status
- source citation fields for single-source governed artifacts
- ledger output paths resolving to existing governed files
- structured evidence tables for multi-source synthesis
- explicit direct/index/relational evidence modes for knowledge cards
- representative-versus-complete evidence coverage declarations
- unresolved generator placeholders in governed Markdown
- missing knowledge-graph links when multiple related governed cards exist
- bundle/section validity for row-level synthesis evidence
- explicit `needs-qa` boundaries around QA-affected evidence
- rejection of authoritative status or evidence levels over QA-affected sections

Read `references/rule-catalog.md` before adding or changing rule codes.

## Reporting

When responding to the user:

1. State the vault linted and profile used.
2. Lead with `status`, error count, warning count, and major metrics.
3. List actionable errors first, then warnings.
4. Distinguish controlled QA from broken state.
5. Suggest the next controlled workflow only when it follows directly from an issue.

If no errors are found but warnings remain, call the result `pass-with-warnings`, not failure.
