# Hermes Obsidian Skills

This repository stores local skills and tool-integration notes for the Hermes + Obsidian knowledge workflow.

## Skills

- `hermes-obsidian-controlled-ingest/`
  - Governed Obsidian ingestion workflow.
  - Protects `10_Raw/` as raw source.
  - Routes outputs into `30_Cards/`, `40_Concepts/`, `50_Projects/`, `90_Dataview/`, or `_system/reports/`.
  - Uses concept registry checks before creating concept pages.
  - Supports MarkItDown as an optional pre-ingestion conversion layer.

- `hermes-obsidian-vault-bootstrap/`
  - Initializes governed Obsidian vaults.
  - Creates the standard folder layout, prompts, templates, metadata registries, Dataview indexes, and setup report.
  - Supports `general` and `meeting` profiles.
  - Can copy `.obsidian/`, base concept pages, and skill notes from a template vault.
  - Does not copy raw sources, test cards, test projects, or historical reports by default.

## Repository Layout

```text
hermes-obsidian-skills/
├── README.md
├── mcp/
│   └── markitdown-mcp.example.json
├── hermes-obsidian-controlled-ingest/
│   ├── SKILL.md
│   ├── agents/
│   │   └── openai.yaml
│   ├── references/
│   └── scripts/
└── hermes-obsidian-vault-bootstrap/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    ├── references/
    └── scripts/
```

## Validation

Validate both skills with:

```powershell
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest"
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-bootstrap"
```

Check helper script syntax with:

```powershell
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_with_markitdown.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-bootstrap\scripts\init_obsidian_vault.py"
```

## MarkItDown Integration

MarkItDown is optional. This repository does not install it automatically.

Two integration modes are planned:

1. Local conversion script:
   - `hermes-obsidian-controlled-ingest/scripts/convert_with_markitdown.py`
   - Converts non-Markdown sources into Markdown before governed ingestion.

2. MCP tool integration:
   - Example config: `mcp/markitdown-mcp.example.json`
   - Actual MCP configuration should live in the Hermes runtime config, not inside the skill.

## Development Rules

- Keep `SKILL.md` concise and stable.
- Put longer operational details in `references/`.
- Put reusable helper code in `scripts/`.
- Do not store secrets, local API keys, or machine-specific credentials in this repository.
- Preserve raw source files; conversion output should go to the intended vault conversion area, such as `10_Raw/converted/`.
