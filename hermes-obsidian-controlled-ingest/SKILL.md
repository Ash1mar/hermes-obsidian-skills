---
name: hermes-obsidian-controlled-ingest
description: Governed Obsidian vault ingestion workflow for Markdown and non-Markdown source files. Use when asked to process files into an Obsidian knowledge vault with raw-source protection, material classification, artifact routing, concept registry checks, Dataview-compatible metadata, ingest logs, source maps for long files, project/spec routing, or MarkItDown-based conversion of PDF/Office/HTML/image/audio inputs before ingestion.
---

# Hermes Obsidian Controlled Ingest

## Overview

Use this skill to turn source files into governed Obsidian vault artifacts without polluting raw sources or overcreating concept pages.

The workflow is:

```text
source file
-> optional MarkItDown conversion
-> 10_Raw preservation
-> material classification
-> artifact routing
-> concept registry check
-> derived artifact
-> ingest log
```

## First Checks

Before writing anything:

1. Read the vault's `AGENTS.md` and `_system/prompts/hermes-ingest-rules.md` if present.
2. Read `_system/metadata/concept-registry.md` before creating or linking concept pages.
3. Inspect existing `30_Cards/`, `40_Concepts/`, `50_Projects/`, and `_system/reports/`.
4. Treat `10_Raw/` as read-only.
5. Preserve source paths in every derived artifact.

For the validated test-vault layout, see `references/vault-structure.md`.

## Source Handling

If the source is already Markdown, process it directly from `10_Raw/`.

If the source is PDF, Word, PowerPoint, Excel, HTML, CSV, JSON, XML, image, audio, EPUB, ZIP, URL, or another non-Markdown format:

1. Convert it to Markdown first.
2. Save converted Markdown under `10_Raw/converted/`.
3. Keep the original file unchanged.
4. Record conversion metadata in the ingest log.

Use `scripts/convert_with_markitdown.py` when a local MarkItDown CLI or Python package is available. See `references/markitdown.md`.

For Agent tool integration through MCP, see `references/mcp-markitdown.md`.

## Material Classification

Do not pre-label the source. Classify from content evidence.

Common classifications:

- short knowledge note
- method or framework material
- tool or product observation
- implementation guide
- project material or project package
- field specification
- terminology material
- quality or validation rule
- long theoretical source
- source-map-only material
- unsuitable for ingestion

Explain the classification before choosing outputs.

## Artifact Routing

Choose output location after classification:

| Judgment | Artifact | Folder |
| --- | --- | --- |
| reusable knowledge idea | knowledge card | `30_Cards/` |
| stable reusable concept | concept page | `40_Concepts/` |
| project package or implementation plan | project note | `50_Projects/` |
| field/specification/rule document | spec index or report | `_system/reports/` |
| long source | source map first | `_system/reports/` |
| dashboard/query view | Dataview note | `90_Dataview/` |
| uncertain or unsafe material | ingest log only | `_system/reports/` |

## Concept Governance

Do not create concept pages by default.

Before creating a concept page, answer:

- Does `concept-registry.md` already cover it?
- Is it only a tool name, product name, model name, field name, module name, step name, chapter title, or one-off feature?
- Is it an implementation case, child scenario, or workflow extension of an existing concept?
- Does it have cross-file reuse value?
- Is current evidence sufficient?
- Is its boundary with existing concepts clear?

If unsure, do not create a concept page. Write a candidate concept review instead.

Use relationship types when linking existing concepts:

- `core`
- `implementation-case`
- `workflow-extension`
- `indirect`
- `contrast`
- `weak`

See `references/concept-governance.md`.

## Long Sources

For long or concept-dense sources:

1. Create a source map first.
2. Do not create cards or concept pages in the same run unless explicitly requested.
3. Split later ingestion by section.
4. Limit card count per run.
5. Put candidate concepts through review before concept-page creation.

## Batch Ingestion

Do not batch ingest mixed material blindly.

For batches:

1. Create a batch pre-scan plan first.
2. Group files by material type.
3. Process one group at a time.
4. Avoid mixing long theory, project docs, tool observations, and field specs in the same write run.

## Required Run Summary

Every run must report:

1. Whether `10_Raw/` remained unchanged.
2. Material classification and evidence.
3. Files created.
4. Possible artifacts not created and why.
5. Whether any concept page was created and why.
6. Whether `concept-registry.md` was updated.
7. Existing concepts reused and relationship types.
8. Candidate concepts and decisions.
9. Recommended next step.

## Output Templates

Use the templates in `references/vault-structure.md` for:

- knowledge cards
- concept pages
- project notes
- spec indexes
- ingest logs
