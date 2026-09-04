# init_obsidian_vault.py Usage

## Basic Meeting Vault

```powershell
python3 "<bootstrap-skill-root>/scripts/init_obsidian_vault.py" `
  --vault-path "<target-vault>" `
  --profile meeting
```

When `config/deployment.json` is packaged with the Skill, its `vault_path` is used and
`--vault-path` may be omitted. Change the checked-in profile only when the deployment changes.

## Use A Template Vault

```powershell
python3 "<bootstrap-skill-root>/scripts/init_obsidian_vault.py" `
  --vault-path "<target-vault>" `
  --profile meeting `
  --template-vault "<template-vault>" `
  --copy-obsidian-config `
  --copy-base-concepts
```

## Engineering Governance Vault

```powershell
python3 "<bootstrap-skill-root>/scripts/init_obsidian_vault.py" `
  --vault-path "<target-vault>" `
  --profile engineering `
  --vault-id "vault-north-plant" `
  --vault-name "North Plant Engineering" `
  --security-domain "internal-a"
```

`--vault-id` and `--security-domain` must be stable lowercase IDs. If `--vault-id` is omitted, the
script creates a UUID-based ID. The display name defaults to the target directory name.

## Safety Behavior

- The script creates directories as needed.
- It refuses to operate on a non-empty target unless `--force-empty` is provided.
- `--force-empty` does not delete files; it only allows writing into an existing directory.
- Existing engineering governance files are never overwritten or upgraded, even with `--force-empty`.
- The script never copies `10_Raw`, `30_Cards`, `50_Projects`, or old `_system/reports` from the template vault.

## Template Copy Rules

From template vault:

- `.obsidian/` is copied only with `--copy-obsidian-config`.
- `40_Concepts/*.md` and `_system/metadata/concept-registry.md` are copied only with `--copy-base-concepts`.

Generated files are profile-specific and written fresh.
