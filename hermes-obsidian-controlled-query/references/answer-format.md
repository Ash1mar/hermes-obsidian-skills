# Answer Format

Use this format for non-trivial controlled queries.

```text
Query type: <locating | explanatory | synthesis | evidence | gap>
Query scope: <folders/files searched>
Main hits:
- <path> - <why it matters> - <evidence label>

Answer:
<concise answer, separating explicit vault/source content from inference>

Evidence:
- Document: <source PDF/manual name>
  Original PDF page: <page number or unresolved>
  Relevant original paragraph: <short quote or tight summary from converted source>
  Figure/table: <none | original PDF page + converted asset path>
  Vault/source path: <source map/ledger/document/table/image path>
  Quality: <clear/source-backed/needs-qa/gap>

Uncertainty / gaps:
- <what is not proven, conflicting, or needs QA>

Writeback recommendation:
<none | create/update card | candidate concept review | source QA | controlled ingest>
```

## Short Locating Answer

For "where did we mention X?" style questions:

```text
Found these main hits:
- <path>: <short context>
- <path>: <short context>

Best starting point: <path>, because <reason>.
Evidence quality: <clear/source-backed/needs-qa/gap>.
```

## Engineering Evidence Answer

For engineering values, formulas, equipment composition, or code-like requirements:

```text
Conclusion: <answer or guarded answer>

Evidence chain:
- Navigation: <source map/spec index/ledger path, section id, page, quality>
- Source text: <document.md/table/image path and excerpt summary>
- User-facing evidence packet: <document name, original PDF page, paragraph, figure/table location>

QA status: <clear/source-backed/needs-qa/gap>
```

Avoid long verbatim copying. Summarize and quote only short phrases needed to anchor the evidence.
