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
```

Do not include writeback recommendations in the user-facing answer by default.
If the user explicitly asks whether to persist the result, briefly state the candidate decision and likely artifact type.

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

## Internal Query-Writeback Candidate

When the query reveals high-value reusable knowledge and candidate logging is allowed, keep or persist this internal handoff for later ingest. It is not part of the normal user answer.

```yaml
type: query-writeback-candidate
status: candidate
user_question: <original question>
answer_summary: <short synthesis, not a source of truth>
candidate_type: <parameter-or-design-value | review-checklist | interface-or-handoff | code-or-principle | object-or-equipment | gap-or-conflict>
evidence_level: <clear | source-backed | needs-qa | gap>
possible_artifact: <none | card | spec-index-update | qa-item | candidate-concept-review | project-note | dataview>
why_candidate: <why this is reusable or review-worthy>
why_not_direct_write: <missing authorization, QA risk, duplicate risk, or boundary uncertainty>
evidence_packets:
  - document: <source PDF/manual name>
    original_pdf_page: <page or unresolved>
    source_path: <source map/ledger/document/table/image path>
    quality: <clear | source-backed | needs-qa | gap>
existing_artifacts_checked:
  - <path or none>
qa_risks:
  - <risk or none>
```

If persisted, place candidates under `_system/reports/query-writeback-candidates/`. Later ingest must re-check source evidence and existing artifacts; it must not treat the query answer summary as source evidence.
