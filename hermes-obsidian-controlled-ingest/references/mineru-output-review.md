# MinerU Output Review and Bundle Regeneration

Use this reference when preserving MinerU output for later review, correcting PDF extraction, or rebuilding a Bundle from reviewed MinerU intermediates. It distinguishes the current converter behavior from the intended review contract.

## Contents

1. Current dependency contract
2. MinerU output roles and retention
3. `content_list.json` and asset references
4. Immutable machine output and reviewed derivatives
5. Structured review operations
6. Heading and paragraph structure corrections
7. Formula, table, and image corrections
8. Source locator and `bbox` policy
9. Candidate Bundle promotion
10. Current implementation gaps

## Current Dependency Contract

The current PDF Bundle converter requires the original PDF for source identity and accepts an existing MinerU output directory through `--from-mineru-output`.

The active transformation inputs are:

```text
original PDF
+ one legacy flat *content_list.json
+ assets referenced by content_list items, normally images/*
```

The converter uses these inputs as follows:

| Input | Current use |
| --- | --- |
| original PDF | validate the source type; record filename, path, and SHA-256; preserve Bundle identity |
| flat `content_list.json` | render `document.md`, page anchors, headings, lists, equations, figures, tables, `blocks.jsonl`, and `outline.json` |
| referenced `images/*` | copy figure assets and optional table page crops into the Bundle |
| MinerU Markdown | fallback only when no flat content list exists; this fallback has incomplete page, block, table, and asset indexing |

The current converter does not use `content_list_v2.json`, `middle.json`, `model.json`, `layout.pdf`, or `span.pdf` to render the Bundle. It copies selected files into `_evidence/` for targeted QA.

Do not claim that editing MinerU Markdown, `content_list_v2.json`, `middle.json`, or `model.json` changes a Bundle produced from a valid flat content list. Under the current implementation, those edits have no rendering effect.

## MinerU Output Roles and Retention

Treat `content_list.json` as a convenient, lossy normalized export, not as a replacement for every MinerU artifact.

Retain three logical layers:

### Source authority

```text
original.pdf
```

The original PDF is the factual and visual authority. Keep it unchanged under the Vault raw-source policy.

### Minimum rebuild inputs

```text
*content_list.json
images/* referenced by content_list
MinerU version and effective conversion settings
```

This layer is sufficient for the current converter to recreate an engineering Bundle when every referenced asset resolves.

### Diagnostic evidence

```text
content_list_v2.json
middle.json
model.json
layout.pdf
span.pdf
MinerU Markdown and logs when retained
```

These files are not ordinary ingest inputs. Use them to determine whether a loss or error arose in model detection, intermediate layout processing, final export, or reading-order reconstruction. `layout.pdf` and `span.pdf` are visual overlays of what MinerU detected; the original PDF alone does not show that machine interpretation.

Keep diagnostic evidence outside default query and ingest context. Open only the specific artifact needed for an extraction dispute.

The converter normally preserves selected JSON/PDF evidence but not the complete original MinerU asset tree. A Bundle whose `_evidence/mineru/` contains `content_list.json` but not the referenced `images/` directory is not, by itself, a complete MinerU rebuild package. Use `--keep-mineru-output` when a complete raw output must be retained, or create a future minimal review package that includes every referenced asset.

## `content_list.json` and Asset References

In the tested MinerU output, `content_list.json` is an ordered JSON array of content blocks. Common item types and fields are:

| Type | Relevant fields |
| --- | --- |
| `text` | `text`, `text_level`, `page_idx`, `bbox` |
| `list` | `list_items`, `page_idx`, `bbox` |
| `equation` | `text`, `page_idx`, `bbox` |
| `image` or `chart` | `image_caption` or `chart_caption`, `img_path`, `page_idx`, `bbox` |
| `table` | `table_caption`, `table_body`, `img_path`, `page_idx`, `bbox` |
| auxiliary layout | `header`, `footer`, `page_number`, `aside_text`, `page_footnote` |

The array order is the current reading order. `page_idx` is zero-based in MinerU output; the converter renders one-based source-page anchors.

An image item references a separate file rather than embedding image bytes:

```json
{
  "type": "image",
  "page_idx": 10,
  "bbox": [120, 260, 920, 710],
  "image_caption": ["图4.3.3-1 消防水池液位设置"],
  "img_path": "images/7d820f6ce6309e48.jpg"
}
```

The converter resolves `img_path` relative to the selected content list, MinerU output root, and optional MinerU Markdown directory. It copies the resolved asset into Bundle `images/` under a normalized name.

A table item normally has both a structured body and an optional visual crop:

```json
{
  "type": "table",
  "page_idx": 11,
  "bbox": [110, 210, 920, 740],
  "table_caption": ["表4.3.4-1 消防泵设计参数"],
  "table_body": "<table>...</table>",
  "img_path": "images/6b942e6cdcaaa4f9.jpg"
}
```

`table_body` becomes a Bundle table Markdown/HTML view. `img_path` supplies the optional page crop used for visual verification. Body-less adjacent table items may represent unresolved cross-page continuations and require review against page evidence.

## Immutable Machine Output and Reviewed Derivatives

Treat the original PDF and the complete original MinerU output as immutable machine evidence:

```text
source/original.pdf                    read-only
mineru-machine/                        read-only
  original_content_list.json
  content_list_v2.json
  middle.json
  model.json
  original.md
  images/
  layout.pdf
  span.pdf
```

Do not edit a machine artifact in place. Apply human corrections in a separate review layer:

```text
review/rev-001/
  changes.jsonl                        authoritative correction log
  review-manifest.yaml                 reviewer and provenance record
  reviewed_content_list.json           generated derivative
  images/                              reviewed asset copies when needed
```

The intended derivation is:

```text
immutable machine content list
+ structured changes.jsonl
-> review compiler
-> reviewed content list and reviewed asset tree
-> Bundle converter
-> candidate Bundle
```

`changes.jsonl` is the human-review authority. The reviewed content list is rebuildable output and must record or link the machine content-list hash and patch hash. Until a review compiler exists, a manually edited copy may be used only as an interim process; keep the original intact and record changes separately.

Assign a stable `origin_id` when the machine content list first enters review. Do not use the current sequential Bundle `block_00001` identifiers as review identity because insertion or deletion shifts later sequence numbers. New human-recovered blocks receive review IDs and record their source relationship.

## Structured Review Operations

The correction log must support semantic and structural operations, not only string replacement:

```text
replace_field
set_heading
change_type
insert_block
delete_block
split_block
merge_blocks
move_block
replace_formula
replace_table
replace_asset
```

Every operation should record:

- stable target `origin_id`, or ordered target IDs for a merge
- operation type
- before value or before hash where practical
- reviewed result
- reason
- reviewer and timestamp
- verification mode, normally `checked_against_pdf`
- page-level source locator and inherited machine locators

Only recover content that is present in the original PDF. Expert interpretation or supplemental knowledge that is not in the source belongs in a governed note, card, or annotation, not in the reviewed source representation.

Deletion is appropriate for extraction artifacts such as duplicated text, headers, footers, or false-positive blocks. Do not delete source content merely because it is irrelevant to the current ingest objective; exclude it during source mapping instead.

## Heading and Paragraph Structure Corrections

### Heading level

Record a reviewed semantic heading depth rather than relying only on MinerU `text_level`:

```json
{
  "operation": "set_heading",
  "target": {"origin_id": "blk_a81d203f"},
  "before": {"heading_depth": 2},
  "after": {"heading_depth": 3},
  "reason": "标题层级识别错误",
  "verification": "checked_against_pdf"
}
```

Distinguish document heading depth from rendered Markdown level. For example, a numbered `6.1.3` heading has semantic depth three, while the current Bundle convention may render it as Markdown level four because the document title occupies level one.

The current converter reconstructs numbered heading levels from the visible number and may override MinerU `text_level`. A future reviewed-output path must apply precedence in this order:

```text
explicit human structure override
-> deterministic numbered-heading reconstruction
-> MinerU text_level
```

Until that override is implemented, changing `text_level` alone cannot reliably enforce a reviewed heading level.

### Split

When one machine block contains a heading and body text, split it into reviewed blocks and record each as derived from the same `origin_id`. Do not invent precise child bboxes when no layout tool recalculated them.

### Merge

When MinerU fragments one paragraph or formula across several blocks, merge them while retaining every original target ID and source locator. Do not collapse several source regions into a fabricated single authoritative bbox.

### Insert and move

An inserted block must identify its position relative to stable block IDs, source page, and verification basis. Use `bbox: null` when only page-level location is known. A moved block retains its original source locator; logical reading order and physical location are separate properties.

## Formula, Table, and Image Corrections

### Formula

Support field replacement, block-type correction, split, and merge. Retain the original page and machine bbox. Record explicit verification against the PDF. A corrected formula must not become authoritative merely because the review file parses successfully; preserve targeted formula QA unless reviewer attestation and policy allow resolution.

### Table

Support caption, HTML/body, header, row, column, unit, merged-cell, continuation, and source-crop corrections. A whole-body replacement may be used initially, but cell-level changes are preferable for auditability. Keep the machine crop immutable; a replacement crop belongs in the review asset tree and must be recorded as a reviewed asset.

### Image

Caption corrections change structured metadata only. When the referenced image or crop is wrong, copy or generate a corrected asset in the review tree and update the reviewed `img_path`. Do not overwrite the machine image or make visual edits that change source meaning.

## Source Locator and `bbox` Policy

`bbox` is machine-generated geometry, not an ordinary human-editable content field. The current converter does not use it to order blocks, determine headings, create page anchors, crop assets, or build the outline. It carries the value into evidence records and asset metadata for source localization and QA.

Therefore:

- preserve the original MinerU `page_idx` and `bbox` as immutable source locators
- do not require reviewers to calculate or type coordinates
- retain the same locator for text, formula, type, and heading corrections tied to the same source region
- record deletion against the original locator without altering it
- for a split, let children share the original source region and mark locator precision as `shared_original_region`
- for a merge, retain a list of original `source_spans`
- for a human-recovered omission, use page-only location and `bbox: null` when no layout tool supplies coordinates
- when a machine bbox is wrong, mark it `unreliable` rather than inventing a replacement

Example review metadata:

```yaml
source_locator:
  page_idx: 10
  bbox: [120, 330, 910, 410]
  generated_by: mineru
locator_review:
  status: unreliable
  note: 边界框包含下一段正文
```

A new coordinate may be recorded only when a layout-aware tool generates it, such as a PDF review UI selection, recropping operation, PDF text-coordinate search, or a deliberate rerun of layout detection. Store it separately as a reviewed locator; never overwrite the machine bbox.

Stable review identity must not depend solely on bbox because the machine geometry can be approximate or wrong.

## Candidate Bundle Promotion

Do not regenerate directly over a valid production Bundle. Use this sequence:

```text
validate reviewed content list and asset references
-> generate a candidate Bundle in a separate directory
-> run validate_document_bundle.py
-> compare source, document, outline, section, table, image, and QA changes
-> retain review provenance and revision hashes
-> promote the candidate only after validation
-> run manage_bundle_ingest.py init to reconcile the section ledger
```

The original PDF SHA remains unchanged for extraction corrections, so Bundle identity remains stable. Changed section content becomes `stale`; unchanged section state and outputs can be preserved by ledger reconciliation. A heading correction may create new section IDs and orphan old ones, so review the candidate diff before promotion.

When using the current `--from-mineru-output` implementation, place exactly one intended `*content_list.json` under the supplied review root. File selection is recursive and heuristic; a directory containing both machine and reviewed lists can select the wrong file.

## Current Implementation Gaps

This reference defines the agreed review contract. The following capabilities are not yet implemented in the current converter and must not be represented as available:

- review compiler that applies `changes.jsonl`
- stable review `origin_id`
- explicit `--content-list`, `--asset-root`, and `--review-manifest` inputs
- human heading-depth override that takes precedence over numbered-heading reconstruction
- structured split, merge, insert, delete, move, and type-change operations
- reviewed locator and locator-reliability metadata
- Bundle revision comparison and atomic candidate promotion
- automatic reconstruction of a review asset tree from an existing Bundle manifest
- reviewer attestation integrated into Bundle QA state

Until these exist, preserve the original output, make corrections only in an isolated copy, validate all asset references, generate a candidate Bundle, and keep a separate human-readable change record.
