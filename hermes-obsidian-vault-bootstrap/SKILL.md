---
name: hermes-obsidian-vault-bootstrap
description: On Hermes, MUST call skill_view for hermes-obsidian-vault-bootstrap before governed Vault setup; on other runtimes, load this skill's full instructions before acting. Use to create, configure, clone, prepare, bootstrap, or reset an Obsidian vault structure with standard folders, raw-source protection, Dataview dashboards, metadata registries, prompts, templates, and profile-specific layouts.
---

# Hermes Obsidian Vault Bootstrap

## Core Rule

Use this skill for vault setup only. Use `hermes-obsidian-controlled-ingest` for processing source files after the vault exists.

## Intranet Vault Configuration

On the `intranet` branch, the target vault path is fixed by this skill's config:

- config file: `config/intranet.json`
- configured vault path: `/opt/data/phq/testVault`

Use that configured path for bootstrap by default. If the server vault path changes, update `config/intranet.json`; do not rely on a prompt-level vault path change.

## Runtime Skill Boundary

Use `<bootstrap-skill-root>` as the runtime-neutral directory containing this active `SKILL.md`. Resolve it from the active runtime's loader. On Hermes, use the concrete expanded `${HERMES_SKILL_DIR}` or the `skill_dir` returned by `skill_view(name="hermes-obsidian-vault-bootstrap")`; on another runtime, use its equivalent active-skill directory.

Resolve bundled `scripts/`, `references/`, and templates against `<bootstrap-skill-root>`. Never infer installation from `~/.hermes/skills` or another conventional path. On Hermes, inspect `skill_view` and its linked files before declaring the package incomplete. Never copy replacement Skill resources into the target Vault.

## Workflow

1. Use the configured intranet vault path and identify the profile.
2. Refuse to overwrite a non-empty target unless the user explicitly asked for overwrite behavior.
3. Create the standard folder layout.
4. Write `AGENTS.md`, `README.md`, prompts, metadata registries, templates, Dataview indexes, and setup report.
5. If requested, copy `.obsidian/` config from a template vault.
6. If requested, copy only base concept pages and concept registry from a template vault.
7. Do not copy raw sources, generated cards, project notes, or historical reports unless explicitly requested.
8. Validate created files and report counts.

## Profiles

Use `general` for normal knowledge vaults.

Use `meeting` when the user says the vault is for meeting minutes, meeting records, action items, decisions, related meeting materials, or recurring workstream meetings.

Profile details are in `references/profiles.md`.

## Scripted Setup

Prefer the bundled script for repeatable initialization:

```powershell
python scripts/init_obsidian_vault.py --profile meeting --template-vault "<template-vault>" --copy-obsidian-config --copy-base-concepts
```

Useful options:

- `--vault-path <path>` for explicit one-off compatibility only; normal intranet deployments should edit `config/intranet.json`
- `--profile general|meeting`
- `--template-vault <path>`
- `--copy-obsidian-config`
- `--copy-base-concepts`
- `--force-empty`

Read `references/script-usage.md` before using advanced options.

## Safety Checks

Before writing:

- Confirm the target path is not an existing non-empty directory unless overwrite was explicitly requested.
- Keep raw source areas empty during bootstrap.
- Do not import test artifacts into a real vault.
- Treat `.obsidian/` as configuration only; do not copy workspace history if it would point to obsolete files unless the script rewrites it.

## Expected Output

After setup, report:

1. configured vault path
2. selected profile
3. copied template components
4. created directories
5. created system files
6. copied base concepts count
7. validation results
8. next prompt the user can give Hermes for ingestion
