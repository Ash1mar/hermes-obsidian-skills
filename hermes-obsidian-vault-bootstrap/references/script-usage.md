# init_obsidian_vault.py Usage

## Basic Meeting Vault

```powershell
python scripts/init_obsidian_vault.py `
  --vault-path "C:\Users\vimdr\Desktop\hermes-workspace\Hermes-MeetingVault" `
  --profile meeting
```

## Use A Template Vault

```powershell
python scripts/init_obsidian_vault.py `
  --vault-path "C:\Users\vimdr\Desktop\hermes-workspace\Hermes-MeetingVault" `
  --profile meeting `
  --template-vault "C:\Users\vimdr\Desktop\hermes-workspace\Hermes-Obsidian-TestVault" `
  --copy-obsidian-config `
  --copy-base-concepts `
  --copy-skill-note
```

## Safety Behavior

- The script creates directories as needed.
- It refuses to operate on a non-empty target unless `--force-empty` is provided.
- `--force-empty` does not delete files; it only allows writing into an existing directory.
- The script never copies `10_Raw`, `30_Cards`, `50_Projects`, or old `_system/reports` from the template vault.

## Template Copy Rules

From template vault:

- `.obsidian/` is copied only with `--copy-obsidian-config`.
- `40_Concepts/*.md` and `_system/metadata/concept-registry.md` are copied only with `--copy-base-concepts`.
- `_system/skills/hermes-obsidian-controlled-ingest.skill.md` is copied only with `--copy-skill-note`.

Generated files are profile-specific and written fresh.
