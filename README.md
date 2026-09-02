# Hermes Obsidian Skills

This repository stores local skills and tool-integration notes for the Hermes + Obsidian knowledge workflow. It also maintains `qmd-like-rag/`, the independently installed coarse-recall Provider used by the ingest/query Skills; it is repository code, not a fifth Skill.

The system-level architecture, data authority, interface, compatibility, and acceptance baseline is defined in [`docs/OFFICIAL_TECHNICAL_SPECIFICATION.md`](docs/OFFICIAL_TECHNICAL_SPECIFICATION.md).

See [`DOCUMENTATION.md`](DOCUMENTATION.md) for a file-by-file documentation map and [`charts.md`](charts.md) for editable Mermaid diagrams of the complete bootstrap, ingest, lint, query, retrieval, and branch-specific deployment flow.

Maintainers should follow [`BRANCH_MAINTENANCE.md`](BRANCH_MAINTENANCE.md): implement shared work on
`main`, then merge `main` into `intranet` while preserving checked-in deployment configuration.

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
  - Runs optional hybrid recall and hierarchical section location in parallel, fuses their union, then performs governed-layer-first traditional search and scoped exact/lexical fallback.
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

## Branch Deployment Profiles

The Skill implementation and UI metadata are shared across both maintained branches. Runtime
differences are checked-in configuration, not duplicated instructions or scripts:

| Branch | Deployment profile |
| --- | --- |
| `main` | No packaged `config/deployment.json`; callers provide the Vault, local MinerU CLI remains the fallback, and viewer links are absent unless explicitly configured. |
| `intranet` | Each applicable Skill packages `config/deployment.json`; the current profile selects Vault `/opt/data/phq/testVault`, Skills parent `/opt/data/skills`, MinerU HTTP `http://10.27.17.35:7861`, and viewer `http://10.27.13.12:8765/viewer`. |

Downloading `intranet` is therefore sufficient for its current deployment. Change the checked-in
profile only when the server deployment changes; ordinary prompts do not need to repeat those
values.

## Hermes Slash Aliases

The `hermes-skill-bundles/` directory contains single-skill Hermes bundles that provide deterministic short slash commands:

- `/v-query` loads `hermes-obsidian-controlled-query`
- `/v-ingest` loads `hermes-obsidian-controlled-ingest`
- `/v-bootstrap` loads `hermes-obsidian-vault-bootstrap`
- `/v-lint` loads `hermes-obsidian-vault-lint`

Hermes normalizes bundle names to lowercase slugs, so use the lowercase forms above. Deploy these YAML files to the active instance's `<HERMES_HOME>/skill-bundles/` directory and run `hermes bundles reload`. The aliases are runtime adapters; canonical skill names and cross-runtime skill-root placeholders remain unchanged.

## Representative Package Layout

The compact tree below illustrates the shared package shape with two representative Skills; it is not an exhaustive inventory. The current four-Skill, Provider, bundle, test, and documentation map is maintained in [`DOCUMENTATION.md`](DOCUMENTATION.md).

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

## qmd-like-rag Provider

`qmd-like-rag/` is derived from the earlier `qmd-rag-v2.1` prototype and retains Markdown chunking, Chroma, BM25, RRF, deduplication, parent expansion, reranking, and incremental fingerprints. Standalone LLM answer generation and the prototype Skill wrapper were removed because controlled-query owns answer synthesis and evidence verification.

The package exposes the `qmd-like-rag` CLI and an optional HTTP server. On `main`, install it into a WSL-native virtual environment and use the Skill adapters' command transport. On `intranet`, do not deploy QMD; install the same tagged qmd-like-rag release locally or configure the branch's Skill adapters to call its HTTP transport.

Both maintained branches now carry qmd-like-rag `0.3.0` and the stable `hermes-coarse-recall/v1` protocol. Provider source and Skills share one Git revision, but deployment remains separate: copying either branch does not install qmd-like-rag, its model dependencies, models, or indexes. A deployment whose query and ingest adapters remain `enabled: false` never invokes the Provider, so an intranet checkout may be upgraded before its Provider runtime is provisioned. See `qmd-like-rag/README.md`.

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

Run tests with:

```powershell
python -m unittest discover "C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\tests"
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
  --reports-dir "/path/to/vault/_system/reports"
```

Hermes uses ledger revisions and per-section content hashes to resume work, prevent duplicate ingestion, and mark changed completed sections as stale.

By default, figures and charts are extracted as visual evidence files and referenced from Markdown. Their internals are not OCR-expanded unless explicitly requested through MinerU image/chart analysis. Engineering formulas, table structure, and figure internals remain review-required before they become authoritative knowledge.

The helper accepts `--mineru-command` or `MINERU_COMMAND`. In the configured WSL runtime, use `/usr/local/bin/mineru`, which selects local model snapshots and delegates to the native WSL virtual environment.

On `main`, the helper falls back to the local MinerU CLI. On `intranet`, checked-in deployment config selects MinerU HTTP at `http://10.27.17.35:7861`, so normal commands need no API flags. A deployment config, `--mineru-api-url`, or `MINERU_API_URL` may select or override HTTP transport; the helper posts to `/file_parse` or `/tasks`, requests a ZIP result, and continues through the same Bundle v2 builder. The ZIP must contain Markdown and content-list outputs; middle/model JSON and referenced images are requested for evidence. Some API deployments do not return `layout.pdf` or `span.pdf`, so those QA overlays may be absent from `_evidence/`.

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

Two integration surfaces are documented:

1. Supported local conversion script:
   - `hermes-obsidian-controlled-ingest/scripts/convert_with_markitdown.py`
   - Converts non-Markdown sources into Markdown before governed ingestion.

2. Optional MCP tool integration:
   - Example config: `mcp/markitdown-mcp.example.json`
   - Actual MCP configuration should live in the Hermes runtime config, not inside the skill.

## Development Rules

- Keep `SKILL.md` concise and stable.
- Put longer operational details in `references/`.
- Put reusable helper code in `scripts/`.
- Give every Python or shell entry point under a Skill's `scripts/` directory a shebang and commit it with Git mode `100755` (`chmod +x`). Use explicit `python3 "<skill-dir>/scripts/<script>.py"` commands in instructions so execution does not depend on shell dispatch.
- Do not store secrets, local API keys, or machine-specific credentials in this repository.
- Preserve raw source files; conversion output should go to the intended vault conversion area, such as `10_Raw/converted/`.
