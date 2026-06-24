# Layered MinerU Document Bundle

Use a layered bundle for engineering PDFs whose headings, page evidence, tables, figures, units, or formulas matter.

## Contents

1. Design goals
2. Bundle profiles
3. Engineering bundle contract
4. Default ingest order
5. Conversion commands
6. Local MinerU environment
7. Quality gates
8. Legacy bundle handling

## Design Goals

Keep controlled ingestion compatible with the validated Markdown workflow:

- Keep one authoritative normalized `document.md`.
- Navigate long sources through stable outline line ranges.
- Load tables and figures only when the selected section needs them.
- Preserve raw MinerU evidence without placing it in default agent context.
- Separate extraction quality from knowledge governance.
- Preserve the original PDF unchanged outside the bundle.

Do not create one Markdown copy per section. Section copies drift from the authoritative document and complicate source tracing.

## Bundle Profiles

### Basic

Use for simple Markdown or simple PDF conversions:

```text
document_bundle/
  manifest.json
  document.md
  images/
```

### Engineering

Use for complex engineering PDFs:

```text
document_bundle/
  manifest.json
  document.md
  outline.json
  images/
  tables/
  _evidence/
    blocks.jsonl
    mineru/
```

The engineering profile is a backward-compatible superset at the file level: `document.md`, `images/`, and `manifest.json` remain available.

## Engineering Bundle Contract

### manifest.json

Read this file first. It records:

- `schema_version`: `2.0`
- `profile`: `engineering`
- source path, filename, SHA-256, and parsed page count
- MinerU version, backend, method, effort, formula/table settings, and model source
- document and outline paths
- image and table inventories
- optional evidence inventory
- feature flags
- content counts
- quality status, issues, and targeted review requirements

Quality status values:

- `pass`: deterministic structural checks passed
- `warn`: source maps are allowed, but affected evidence must not become authoritative knowledge
- `fail`: stop downstream ingestion and create only a QA/ingest report

### document.md

Treat this as the single normalized text source. It includes invisible page anchors:

```markdown
<!-- source-page: 11 -->

#### 4.3.3 消防水池参数的确定
```

Use page anchors and outline line ranges for traceability. Do not remove or rewrite them downstream.

Figures remain Markdown links:

```markdown
![图4.3.3-1 消防水池液位设置](images/fig_4_3_3_1.jpg)
```

Complex tables are linked rather than inserted as one oversized HTML line:

```markdown
**表4.3.4-1 消防泵、稳压泵、循环泵设计参数**

[查看表格](tables/table_4_3_4_1.md)

<!-- table-id: table_4_3_4_1; source-page: 12; qa: pass -->
```

Block formulas remain in the document with invisible evidence markers. Treat formula semantics as review-required even when LaTeX is syntactically valid.

### outline.json

Use the outline to select bounded ranges without reading the entire source:

```json
{
  "schema_version": "2.0",
  "document": "document.md",
  "sections": [
    {
      "id": "4.3.3",
      "title": "4.3.3 消防水池参数的确定",
      "level": 4,
      "parent": "4.3",
      "start_line": 301,
      "end_line": 325,
      "pages": [10, 11],
      "assets": ["fig_4_3_3_1"],
      "quality": "pass"
    }
  ]
}
```

The converter reconstructs numbered heading levels when MinerU emits a collapsed level. It demotes labels such as `1.` and `（1）` from structural headings.

### images/

Keep extracted figures and charts as visual evidence. The default conversion does not describe their internals.

When figure internals are required:

1. Keep the original image unchanged.
2. Perform targeted image analysis only for selected figures.
3. Mark generated descriptions as derived and review-required.
4. Do not treat chart values or flow logic as authoritative without visual verification.

### tables/

Each complex table receives:

- a Markdown/HTML view such as `table_4_3_4_1.md`
- an optional page crop such as `table_4_3_4_1_source.jpg`

Do not load all tables during source mapping. Load a table only when the selected section references it.

Body-less consecutive MinerU table blocks are marked as unresolved possible continuations. Verify cross-page structure against the page evidence before extracting rows, parameters, or formulas.

### _evidence/

This is a non-default QA layer. It may contain:

- `blocks.jsonl`
- MinerU `content_list.json`
- `content_list_v2.json`
- `middle.json`
- `model.json`
- `layout.pdf`
- `span.pdf`

Do not recursively scan `_evidence/` during ordinary ingestion. Open only the specific evidence file needed to resolve a layout, page-order, formula, table, or extraction dispute.

## Default Ingest Order

```text
manifest.json
-> bundle validator
-> initialize/reconcile source map + section ledger
-> outline.json
-> claim one ledger section with revision checking
-> selected non-overlapping ledger content_ranges from document.md
-> linked table/image only when needed
-> _evidence only for targeted QA
-> record outputs and terminal section status
```

Validate with:

```bash
python hermes-obsidian-controlled-ingest/scripts/validate_document_bundle.py \
  "/path/to/document_bundle"
```

The validator exits:

- `0` for pass
- `1` for warn
- `2` for fail

For staged ingestion, resumption, deduplication, and Bundle change detection, continue with `scripts/manage_bundle_ingest.py` and read `bundle-source-map-ledger.md`.

## Conversion Commands

Run from WSL. Keep the heavy MinerU/vLLM environment on the WSL native filesystem; the bundle helper itself can run with the system Python:

```bash
cd /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills

python3 \
  hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py \
  "/mnt/c/path/to/input.pdf" \
  -o "/mnt/c/path/to/vault/10_Raw/converted/input_document_bundle" \
  --mineru-command "/usr/local/bin/mineru" \
  --model-source local \
  --backend hybrid-engine \
  --effort high \
  --method auto \
  --lang ch \
  --overwrite
```

For born-digital PDFs, compare `--method txt` with `auto` on representative pages before a full run.

For a CPU fallback:

```bash
python3 \
  hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py \
  "/mnt/c/path/to/input.pdf" \
  -o "/mnt/c/path/to/vault/10_Raw/converted/input_document_bundle" \
  --mineru-command "/usr/local/bin/mineru" \
  --model-source local \
  --backend pipeline \
  --method auto \
  --lang ch \
  --overwrite
```

Defaults:

- formula parsing enabled
- table parsing enabled
- image analysis disabled
- selected evidence files preserved
- complete raw MinerU output discarded unless `--keep-mineru-output` is supplied

Use `--no-evidence` only when storage constraints explicitly outweigh later QA requirements.

Reuse existing MinerU output without reparsing:

```bash
python3 \
  hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py \
  "/mnt/c/path/to/input.pdf" \
  -o "/mnt/c/path/to/input_document_bundle" \
  --from-mineru-output "/mnt/c/path/to/mineru/output" \
  --overwrite
```

Reused output records conversion settings as unknown by default. If the original invocation is known, add `--record-conversion-settings` together with the original `--backend`, `--method`, feature flags, effort, and model source so the manifest retains accurate provenance.

## Local MinerU Environment

Expected local CLI and environment:

```text
/usr/local/bin/mineru
/root/.venvs/mineru
```

Use MinerU 3.3.1 or a tested later version. Keep the venv MinerU-only; do not install `hermes-agent` into it. Do not place a vLLM-enabled environment under `/mnt/c`, because WSL Plan 9 file access can make cold starts appear hung in `p9_client_rpc`.

For an offline-reproducible environment:

1. Download complete pipeline and VLM model snapshots from an accessible source.
2. Configure `/root/mineru.json` with local `pipeline` and `vlm` model roots.
3. Run conversions with `--model-source local`.
4. Verify `pip check`, CUDA availability, pipeline smoke test, and hybrid smoke test.

Do not rely on HuggingFace at conversion time when the network has TLS or proxy instability. ModelScope may be used to populate the local model root; the conversion itself should still use `local`.

## Quality Gates

Before source mapping, check:

- source filename, bundle name, and source hash agree
- chapter order and reconstructed hierarchy are plausible
- page anchors cover the parsed pages
- headers, footers, and page numbers do not pollute body text
- table files exist and possible cross-page continuations are flagged
- all important image references resolve
- formulas and units selected for knowledge extraction have page evidence
- `_evidence` is excluded from default ingestion

Before promoting engineering facts, manually or deterministically verify:

- formulas and variable definitions
- units, superscripts, subscripts, and scientific notation
- table headers, merged cells, row continuation, and table formulas
- chart values and flow-diagram logic

## Legacy Bundle Handling

Bundle v1 contains only `document.md`, `images/`, and `manifest.json`.

For v1:

1. Validate image references and document identity.
2. Allow a coarse source map.
3. Do not treat unanchored tables, formulas, or parameters as authoritative.
4. Prefer reconversion to v2 when the original PDF and MinerU environment are available.
