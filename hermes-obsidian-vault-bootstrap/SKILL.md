---
name: hermes-obsidian-vault-bootstrap
description: Vault 初始化 / Vault Bootstrap：创建、配置、准备、克隆或重置受治理 Obsidian Vault 结构时使用，包括标准目录、原始来源保护、Dataview、注册表、提示和模板。On Hermes, MUST call skill_view for hermes-obsidian-vault-bootstrap and load its complete scripts before setup; on other runtimes, load the full skill first. Do not use for ordinary source ingest.
---

# Hermes Obsidian Vault Bootstrap

## Core Rule

Use this skill for vault setup only. Use `hermes-obsidian-controlled-ingest` for processing source files after the vault exists.

## Deployment Profile

Resolve the target from an explicit `--vault-path` or optional `config/deployment.json`. A packaged
`vault_path` is the normal deployment default; an explicit path is a deliberate one-off override.
Treat `hermes_skills_root` only as a consistency check for the loader-returned Skill directory.
When deployment config is absent, require an explicit target path.

## Runtime Skill Boundary

Use `<bootstrap-skill-root>` as the runtime-neutral directory containing this active `SKILL.md`, not the parent directory that contains multiple Skills. The package layout is `<bootstrap-skill-root>/SKILL.md`, `<bootstrap-skill-root>/scripts/*.py`, `<bootstrap-skill-root>/references/*.md`, and optional `<bootstrap-skill-root>/config/*.json`. Resolve it from the active runtime's loader. On Hermes, use the concrete expanded `${HERMES_SKILL_DIR}` or the `skill_dir` returned by `skill_view(name="hermes-obsidian-vault-bootstrap")`; on another runtime, use its equivalent active-skill directory.

Resolve bundled `scripts/`, `references/`, `config/`, and templates against `<bootstrap-skill-root>`. Execute Python entry points as `python3 "<bootstrap-skill-root>/scripts/<script>.py"`. Never infer a conventional installation path. On Hermes, inspect `skill_view` and its linked files before declaring the package incomplete. Never copy replacement Skill resources or runtime installation paths into the target Vault.

## Workflow

1. Resolve the target vault path and identify the profile.
2. Refuse to overwrite a non-empty target unless the user explicitly asked for overwrite behavior.
3. Create the standard folder layout.
4. Write `AGENTS.md`, `README.md`, prompts, metadata registries, templates, Dataview indexes, and setup report.
   For `engineering`, also create the revision-0 JSON governance control plane and database mapping.
5. If requested, copy `.obsidian/` config from a template vault.
6. If requested, copy only base concept pages and concept registry from a template vault.
7. Do not copy raw sources, generated cards, project notes, or historical reports unless explicitly requested.
8. Validate created files and report counts.

## Profiles

Use `general` for normal knowledge vaults.

Use `meeting` when the user says the vault is for meeting minutes, meeting records, action items, decisions, related meeting materials, or recurring workstream meetings.

Use `engineering` when document identity, multiple source organizations, business versions, authority,
or future storage/database migration must be governed. Its control plane starts as `readiness: draft`.

Profile details are in `references/profiles.md`. Read `references/governance-control-plane.md` before
creating or maintaining an engineering governance control plane.

## Scripted Setup

Prefer the bundled script for repeatable initialization:

```powershell
python3 "<bootstrap-skill-root>/scripts/init_obsidian_vault.py" --vault-path "<target-vault>" --profile meeting --template-vault "<template-vault>" --copy-obsidian-config --copy-base-concepts
```

When a deployment packages `config/deployment.json` beside the Skill, its
`vault_path` is the default and `--vault-path` may be omitted. An explicit path
always wins. This keeps packages without deployment config portable while configured
packages remain ready to run immediately.

Useful options:

- `--vault-path <path>` for an explicit target or deliberate deployment override
- `--profile general|meeting|engineering`
- `--vault-id <stable-id>`, `--vault-name <display-name>`, and `--security-domain <stable-id>` for engineering
- `--template-vault <path>`
- `--copy-obsidian-config`
- `--copy-base-concepts`
- `--force-empty`

Read `references/script-usage.md` before using advanced options.

## Safety Checks

Before writing:

- Confirm the target path is not an existing non-empty directory unless overwrite was explicitly requested.
- Keep raw source areas empty during bootstrap.
- Never overwrite or upgrade existing engineering governance JSON, including with `--force-empty`.
- Do not treat `readiness: draft` as production approval.
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
8. governance backend and readiness for the engineering profile
9. next prompt the user can give Hermes for ingestion
