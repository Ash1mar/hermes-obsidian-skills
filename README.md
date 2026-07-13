# Hermes Obsidian Skills

This repository stores local skills and tool-integration notes for the Hermes + Obsidian knowledge workflow.

## Skills

- `hermes-obsidian-controlled-ingest/`
  - Governed Obsidian ingestion workflow.
  - Protects `10_Raw/` as raw source.
  - Routes outputs into `30_Cards/`, `40_Concepts/`, `50_Projects/`, `90_Dataview/`, or `_system/reports/`.
  - Uses concept registry checks before creating concept pages.
  - Supports layered MinerU document bundle v2 for engineering manuals, with a small agent-facing Markdown/outline contract and a non-default QA evidence layer.
  - Supports standalone image Bundle v2 for scanned pages, table screenshots, diagrams, and other image-only sources with OCR review controls.
  - Supports MarkItDown as an optional pre-ingestion conversion layer for non-PDF sources and simple fallback conversion.

- `hermes-obsidian-controlled-query/`
  - Governed read-only query workflow for existing Hermes + Obsidian vaults.
  - Searches governed layers before source reports and converted raw bundles.
  - Distinguishes vault facts, source-backed evidence, agent inference, QA risks, and gaps.
  - Prevents query runs from creating or modifying cards, concepts, reports, or raw material unless writeback is explicitly requested.

- `hermes-obsidian-vault-lint/`
  - Audits governed Obsidian vault health without assuming a fixed vault path.
  - Checks bootstrap structure, bundle validation, section ledgers, source maps, governed Markdown evidence, structured multi-source synthesis, and QA authority boundaries.
  - Provides `post-ingest`, `query-ready`, `strict`, and `qa-review` profiles with stable JSON and Markdown report output.
  - Reuses the controlled-ingest bundle validator when available.

- `hermes-obsidian-vault-bootstrap/`
  - Initializes governed Obsidian vaults.
  - Creates the standard folder layout, prompts, templates, metadata registries, Dataview indexes, and setup report.
  - Supports `general` and `meeting` profiles.
  - Can copy `.obsidian/`, base concept pages, and skill notes from a template vault.
  - Does not copy raw sources, test cards, test projects, or historical reports by default.

On the `intranet` branch, bootstrap, ingest, and query use the vault path configured in each skill's `config/intranet.json`, currently `/opt/data/phq/testVault`. Vault lint remains prompt-scoped and requires `--vault` so it can audit any requested vault.

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

Validate skills with:

```powershell
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest"
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-query"
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-bootstrap"
python "C:\Users\vimdr\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-lint"
```

Check helper script syntax with:

```powershell
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_pdf_with_mineru_bundle.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_image_with_ocr_bundle.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\validate_document_bundle.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\manage_bundle_ingest.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-controlled-ingest\scripts\convert_with_markitdown.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-bootstrap\scripts\init_obsidian_vault.py"
python -m py_compile "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\hermes-obsidian-vault-lint\scripts\lint_vault.py"
```

## MinerU PDF Bundle Integration

MinerU is optional. This repository does not install it automatically.

Prefer installing MinerU in the Hermes runtime environment. For WSL2-based Hermes, keep the MinerU virtual environment on the WSL native filesystem (for example `/root/.venvs/mineru`) rather than under `/mnt/c`; vLLM imports and CUDA extension loading are severely I/O-bound on the Windows mount.

Use it for engineering PDFs and complex manuals where layout, formulas, tables, figures, page evidence, and section hierarchy matter.

Main helper:

- `hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py`
- `hermes-obsidian-controlled-ingest/scripts/validate_document_bundle.py`
- `hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py`

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

`document.md` remains the single normalized text source. Hermes reads `manifest.json`, then `outline.json`, then the selected section's non-overlapping ledger `content_ranges`. Parent scopes remain useful for navigation without causing duplicate child ingestion. Tables and figures are loaded only when referenced by that section. `_evidence/` preserves selected MinerU QA artifacts but is excluded from default ingestion.

For staged or multi-session ingestion, initialize a human-readable source map and a machine-readable section ledger under the vault `_system/reports/` directory:

```bash
python3 hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py \
  init "/path/to/input_document_bundle" \
  --reports-dir "/opt/data/phq/testVault/_system/reports"
```

Hermes uses ledger revisions and per-section content hashes to resume work, prevent duplicate ingestion, and mark changed completed sections as stale.

By default, figures and charts are extracted as visual evidence files and referenced from Markdown. Their internals are not OCR-expanded unless explicitly requested through MinerU image/chart analysis. Engineering formulas, table structure, and figure internals remain review-required before they become authoritative knowledge.

The helper accepts `--mineru-command` or `MINERU_COMMAND`. In the configured WSL runtime, use `/usr/local/bin/mineru`, which selects local model snapshots and delegates to the native WSL virtual environment.

On the `intranet` branch, the helper defaults to the MinerU 3.x HTTP API at `http://10.27.17.35:7861`, so prompts do not need to repeat the API URL. Override it with `--mineru-api-url` or `MINERU_API_URL` if the service address changes. The helper posts to `/file_parse` or `/tasks`, requests `response_format_zip=true`, extracts the returned ZIP, and continues with the same Bundle v2 builder. The API ZIP must include at least Markdown and content-list outputs; middle/model JSON and images are requested for evidence. Some API deployments do not include `layout.pdf` or `span.pdf`, so those QA PDFs may be absent from `_evidence/`. To force a local MinerU CLI on this branch, pass `--mineru-invocation cli`.

## Standalone Image Bundle Integration

Use `hermes-obsidian-controlled-ingest/scripts/convert_image_with_ocr_bundle.py` for image-only sources where the image itself carries source content, such as scanned pages, table screenshots, diagrams, forms, or image-based specifications.

It creates a compatible Bundle v2:

```text
image_document_bundle/
  manifest.json
  document.md
  outline.json
  images/
  _evidence/
```

The image remains the visual evidence. OCR text can be supplied with `--ocr-text-file` or generated through `--ocr-command`, but the bundle remains `warn` with review requirements so OCR, table structure, and diagram internals are not promoted as authoritative facts without QA. After conversion, use the same `validate_document_bundle.py` and `manage_bundle_ingest.py init` flow as MinerU PDF bundles. See `hermes-obsidian-controlled-ingest/references/image-bundle.md`.

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
