# Concept Governance

Concept pages are expensive. Create fewer, better concept pages.

## Default

Default to:

- reuse existing concepts
- create a knowledge card
- write a candidate concept review
- defer concept-page creation

Do not default to creating `40_Concepts/` files.

## Create A Concept Page Only If

All are true:

1. Existing registry entries do not cover it.
2. It is not a product, tool, model, field, step, module, or section title.
3. It is not merely a case or implementation of an existing concept.
4. It has clear cross-file reuse value.
5. Current evidence is sufficient.
6. Its boundary with existing concepts is clear.

## Candidate Decisions

- `candidate`: likely valid, but should not be created in this run.
- `defer`: plausible, but needs more evidence or boundary work.
- `reject`: not suitable as a concept page.

## Relationship Types

- `core`: central to the source.
- `implementation-case`: source is a concrete implementation of the concept.
- `workflow-extension`: source extends or operationalizes the concept.
- `indirect`: related but not central.
- `contrast`: useful as a comparison or alternative.
- `weak`: only tangentially related.

## Anti-Patterns

Avoid:

- creating graph nodes for visual density
- creating pages for every repeated noun
- creating pages from table fields
- creating pages from tool names
- creating pages from headings
- creating near-duplicates such as "AI knowledge base", "local AI knowledge base", and "personal AI knowledge base" without boundary analysis

