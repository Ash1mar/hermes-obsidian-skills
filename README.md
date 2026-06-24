# Hermes Obsidian Skills

This repository stores local skills and tool-integration notes for the Hermes + Obsidian knowledge workflow.

## Skills

- `hermes-obsidian-controlled-ingest/`
  - Governed Obsidian ingestion workflow.
  - Protects `10_Raw/` as raw source.
  - Routes outputs into `30_Cards/`, `40_Concepts/`, `50_Projects/`, `90_Dataview/`, or `_system/reports/`.
  - Uses concept registry checks before creating concept pages.
  - Supports layered MinerU document bundle v2 for engineering manuals, with a small agent-facing Markdown/outline contract and a non-default QA evidence layer.
  - Supports MarkItDown as an optional pre-ingestion conversion layer for non-PDF sources and simple fallback conversion.

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
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_pdf_with_mineru_bundle.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\validate_document_bundle.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_with_markitdown.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-bootstrap\scripts\init_obsidian_vault.py"
```

## MinerU PDF Bundle Integration

MinerU is optional. This repository does not install it automatically.

Prefer installing MinerU in the Hermes runtime environment. For WSL2-based Hermes, keep the MinerU virtual environment on the WSL native filesystem (for example `/root/.venvs/mineru`) rather than under `/mnt/c`; vLLM imports and CUDA extension loading are severely I/O-bound on the Windows mount.

Use it for engineering PDFs and complex manuals where layout, formulas, tables, figures, page evidence, and section hierarchy matter.

Main helper:

- `hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py`
- `hermes-obsidian-controlled-ingest/scripts/validate_document_bundle.py`

It creates a layered bundle:

```text
document_bundle/
  manifest.json
  document.md
  outline.json
  images/
  tables/
  _evidence/
```

`document.md` remains the single normalized text source. Hermes reads `manifest.json`, then `outline.json`, then a selected `document.md` line range. Tables and figures are loaded only when referenced by that range. `_evidence/` preserves selected MinerU QA artifacts but is excluded from default ingestion.

By default, figures and charts are extracted as visual evidence files and referenced from Markdown. Their internals are not OCR-expanded unless explicitly requested through MinerU image/chart analysis. Engineering formulas, table structure, and figure internals remain review-required before they become authoritative knowledge.

The helper accepts `--mineru-command` or `MINERU_COMMAND`. In the configured WSL runtime, use `/usr/local/bin/mineru`, which selects local model snapshots and delegates to the native WSL virtual environment.

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
