# Evidence Levels

Use evidence labels to prevent a query answer from overstating weak or extraction-sensitive material.

## Labels

| Label | Meaning | Allowed use |
| --- | --- | --- |
| `clear` | A governed artifact or pass-quality source section supports the answer and resolves to an original PDF identity and page. | Can answer with original-PDF evidence. |
| `source-backed` | Internally checked converted text supports the answer and resolves to an original PDF identity and page, but no durable card or concept has captured it yet. | Can answer with original-PDF evidence; do not cite the conversion carrier. |
| `needs-qa` | The answer depends on formula OCR, engineering values, cross-page tables, figure internals, image evidence, or a section marked `qa_required`/warning-affected. | Do not present as final authority; recommend page/table/figure verification. |
| `gap` | The vault lacks enough evidence in governed layers and converted sources. | Answer with missing material and next lookup/ingest action. |

## Quality Rules

- Do not promote formulas, table rows, figure internals, or parameters from a `qa_required` section into reusable facts.
- Do not treat an ingest log or spec index as the final source for a design value unless it quotes or points to checked source evidence.
- Do not collapse project-specific values into a cross-project rule without comparing multiple sources and noting exceptions.
- Use source maps and ledgers internally to resolve status, section, original PDF identity, page, and quality; record their paths in the query trace.
- Record converted source, table, image, and page-image paths and line/section locations in the query trace only. Do not expose them as user-facing evidence sources.
- For every user-facing substantive answer, cite the original PDF identity and original PDF page rather than converted Markdown lines.
- If a figure, page image, or table is part of the evidence, include its original PDF page, number or caption, containing section, and page region. Include coordinates only when reliably mapped to the original PDF. Keep converted asset paths in the trace. If its internals were not reviewed, mark it `needs-qa`.
- When `inspect` marks original-page verification as required, run the deterministic `verify` preparation once. If it returns `unavailable` or `failed`, stop trying alternative PDF/text tools, preserve the returned unresolved reason, and use `needs-qa` or `gap`; do not use `clear` or `source-backed`.
- When visual verification was explicitly required for a query, `clear` requires every cited packet reference to appear in `verified_evidence_refs` and in a completed `page-asset-verification` event with the actual inspected carrier path. The session script never infers this requirement from domain terms.

## Conflict Handling

When two sources disagree:

1. List both sources.
2. Note project/document identity and page/section.
3. Prefer the latest or project-specific source only if the governance files justify that precedence.
4. Otherwise answer that the vault contains conflicting evidence.

## Missing Evidence

Use `gap` when:

- the query asks for a norm/code that is not present in the vault
- only table/figure images exist and they have not been reviewed
- the relevant source has not been ingested or converted
- the governed layer has a candidate note but no confirmed artifact
- the answer cannot be traced to an original PDF identity and original PDF page
