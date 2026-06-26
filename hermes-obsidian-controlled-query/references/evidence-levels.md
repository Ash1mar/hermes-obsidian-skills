# Evidence Levels

Use evidence labels to prevent a query answer from overstating weak or extraction-sensitive material.

## Labels

| Label | Meaning | Allowed use |
| --- | --- | --- |
| `clear` | A governed card/concept/project or pass-quality source section directly supports the answer. | Can answer directly with citation. |
| `source-backed` | Converted source text supports the answer, but no durable card or concept has captured it yet. | Can answer, but note it is not yet synthesized into a governed artifact. |
| `needs-qa` | The answer depends on formula OCR, engineering values, cross-page tables, figure internals, image evidence, or a section marked `qa_required`/warning-affected. | Do not present as final authority; recommend page/table/figure verification. |
| `gap` | The vault lacks enough evidence in governed layers and converted sources. | Answer with missing material and next lookup/ingest action. |

## Quality Rules

- Do not promote formulas, table rows, figure internals, or parameters from a `qa_required` section into reusable facts.
- Do not treat an ingest log or spec index as the final source for a design value unless it quotes or points to checked source evidence.
- Do not collapse project-specific values into a cross-project rule without comparing multiple sources and noting exceptions.
- For source maps and ledgers, cite status, section id, title, page, and quality when relevant.
- For converted source text, cite the bundle/source path and line or section when possible.
- For user-facing engineering answers, prefer original PDF page numbers from source maps/ledgers over converted Markdown line numbers.
- If a figure/table is part of the evidence, include both its original PDF page and converted asset path. If the asset exists but its internals were not reviewed, mark it `needs-qa`.

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
