# MarkItDown Integration

Use MarkItDown as a pre-ingestion conversion layer for non-PDF sources and simple PDF fallback.

## Role In This Workflow

```text
Office / HTML / image / audio / EPUB / ZIP / URL / simple PDF fallback
-> MarkItDown
-> Markdown under 10_Raw/converted/
-> governed Hermes ingestion
```

MarkItDown should not decide knowledge structure. It only normalizes source format into Markdown.

For engineering PDFs and complex manuals, prefer the MinerU bundle workflow in `mineru-pdf-bundle.md`.

## Supported Source Types

The local source material describes MarkItDown as supporting PDF, PowerPoint, Word, Excel, images with metadata/OCR options, audio transcription options, HTML, CSV, JSON, XML, ZIP traversal, YouTube URLs, EPUB, and other text-oriented formats.

Capabilities depend on installed optional extras and plugins.

## Local CLI Pattern

```bash
markitdown input.pdf -o output.md
```

or:

```bash
markitdown input.pdf > output.md
```

## Python Pattern

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)
result = md.convert("input.pdf")
print(result.text_content)
```

## Safety Rules

- Preserve the original file.
- Write converted Markdown to `10_Raw/converted/`.
- Record original path, converted path, and conversion method in the ingest log.
- Do not create knowledge cards from a non-Markdown source until conversion output is inspected.
- Do not assume high-fidelity layout preservation; MarkItDown is for LLM-friendly Markdown, not final publishing layout.

## Optional Features

Optional extras can be installed by capability, such as PDF, DOCX, PPTX, XLSX, audio transcription, YouTube transcription, and Azure Document Intelligence support.

Do not install dependencies during ingestion unless the user explicitly asks.
