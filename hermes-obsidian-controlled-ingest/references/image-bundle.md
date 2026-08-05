# Standalone Image Bundle

Use this workflow when the source material is an image file, not a PDF page that merely contains figures.

## Contents

1. [Decision rule](#decision-rule)
2. [Bundle contract](#bundle-contract)
3. [Conversion commands](#conversion-commands)
4. [Quality gates](#quality-gates)
5. [Ingest procedure](#ingest-procedure)

## Decision Rule

Do not use the PDF figure rule for standalone images.

PDF figures are usually supporting evidence because the PDF bundle already has text blocks, page anchors, tables, and headings. A standalone image may be the only source layer. When it carries text, tables, forms, scanned pages, screenshots, diagrams, or drawings, create an image Bundle v2 so the same validator, source map, and section ledger controls apply.

Classify before conversion:

| Image source | Default handling |
|---|---|
| scanned page or screenshot with text | create image bundle; OCR text is derived and review-required |
| table screenshot | create image bundle; table structure is review-required |
| figure, flowchart, drawing, or system diagram | create image bundle; visual internals remain review-required |
| photo used only as evidence | preserve as asset; create a bundle only when it must be cited as a standalone source |

## Bundle Contract

The image bundle uses the same Bundle v2 surface as the MinerU PDF bundle:

```text
image_document_bundle/
  manifest.json
  document.md
  outline.json
  images/
    page_001.png
  _evidence/
    ocr_blocks.jsonl
```

`document.md` keeps one page anchor per image:

```markdown
<!-- source-page: 1 -->

# Image page 1

![Image page 1](images/page_001.png)

<!-- ocr-derived: true; qa: review -->

## OCR Text
```

When OCR text is unavailable, the page remains visual evidence:

```markdown
<!-- image-only-source: true; qa: review -->

OCR text is unavailable. Treat this page as visual evidence until targeted OCR or manual review is completed.
```

`outline.json` contains one section per image page. Sections are marked `quality: warn` so the section ledger initializes them as `qa_required`.

`manifest.json` records:

- `schema_version: 2.0`
- `profile: engineering`
- source path, filename, SHA-256, page count, and `source_type: standalone-image`
- conversion engine and optional OCR command/text sidecars
- image inventory
- `_evidence/ocr_blocks.jsonl`
- `quality.status: warn`
- review requirements such as `image-source-visual-review`, `ocr-text-verification`, `table-structure`, or `figure-internals-when-used-as-evidence`

## Conversion Commands

Create a bundle without OCR text:

```bash
python3 "<ingest-skill-root>/scripts/convert_image_with_ocr_bundle.py" \
  "/path/to/source.png" \
  -o "/opt/data/phq/testVault/10_Raw/converted/source_image_document_bundle" \
  --material-type scanned-page
```

Create a bundle from an OCR sidecar:

```bash
python3 "<ingest-skill-root>/scripts/convert_image_with_ocr_bundle.py" \
  "/path/to/source.png" \
  -o "/opt/data/phq/testVault/10_Raw/converted/source_image_document_bundle" \
  --material-type scanned-page \
  --ocr-text-file "/path/to/source.ocr.txt"
```

Create a multi-image bundle:

```bash
python3 "<ingest-skill-root>/scripts/convert_image_with_ocr_bundle.py" \
  "/path/to/page-001.png" "/path/to/page-002.png" \
  -o "/opt/data/phq/testVault/10_Raw/converted/source_image_document_bundle" \
  --material-type mixed
```

Use `--ocr-command "ocr-tool {input}"` only when the command prints UTF-8 OCR text to stdout. The command is recorded in the manifest, but the OCR text is still derived evidence requiring review.

## Quality Gates

Standalone image bundles intentionally validate as `warn`, not `pass`, unless a future workflow adds human review metadata. This keeps them usable for source mapping while preventing automatic promotion to authoritative facts.

Rules:

- OCR text is derived, not authoritative.
- Table screenshots require table-structure QA before extracting rows, units, formulas, or engineering parameters.
- Figure, chart, flowchart, and drawing internals require visual review before extracting logic, values, topology, or operational rules.
- Missing OCR text is acceptable for a visual-evidence bundle, but downstream ingestion must not pretend the page has normalized text.
- `_evidence/ocr_blocks.jsonl` is a QA layer, not default ingest content.

## Ingest Procedure

After conversion, use the normal layered bundle flow:

```bash
python3 "<ingest-skill-root>/scripts/validate_document_bundle.py" \
  "/path/to/source_image_document_bundle"

python3 "<ingest-skill-root>/scripts/manage_bundle_ingest.py" \
  init "/path/to/source_image_document_bundle" \
  --reports-dir "/opt/data/phq/testVault/_system/reports"
```

Then:

1. Read `manifest.json` first.
2. Honor `review_required`.
3. Use the ledger section and `content_ranges` for traceability.
4. Load the linked image only for the selected section.
5. Put uncertain OCR, table structure, and diagram interpretation into `qa_items`.
6. Mark the section `ingested` only when created outputs explicitly state the OCR/visual review limits, or keep it `qa_required` with a concrete review note.
