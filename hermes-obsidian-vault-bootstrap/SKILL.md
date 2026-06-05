---
name: hermes-obsidian-vault-bootstrap
description: Initialize governed Obsidian vaults for Hermes workflows. Use when asked to create, configure, clone, prepare, bootstrap, or reset an Obsidian vault structure with standard folders, raw-source protection, Dataview dashboards, metadata registries, prompts, templates, optional base concept pages, optional Obsidian config copying, and profile-specific layouts such as meeting-minutes vaults.
---

# Hermes Obsidian Vault Bootstrap

## Core Rule

Use this skill for vault setup only. Use `hermes-obsidian-controlled-ingest` for processing source files after the vault exists.

## Workflow

1. Identify the target vault path and profile.
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
python scripts/init_obsidian_vault.py --vault-path "<target-vault>" --profile meeting --template-vault "<template-vault>" --copy-obsidian-config --copy-base-concepts
```

Useful options:

- `--profile general|meeting`
- `--template-vault <path>`
- `--copy-obsidian-config`
- `--copy-base-concepts`
- `--copy-skill-note`
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

1. vault path
2. selected profile
3. copied template components
4. created directories
5. created system files
6. copied base concepts count
7. validation results
8. next prompt the user can give Hermes for ingestion
