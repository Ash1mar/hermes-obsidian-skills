# Answer Format

Use this format for non-trivial controlled queries.

```text
Query type: <locating | explanatory | synthesis | evidence | gap>
Original-PDF scope: <source PDF documents covered>
Main hits:
- <original PDF name, original PDF page> - <why it matters> - <evidence label>

Answer:
<concise answer, separating explicit vault/source content from inference>

Evidence:
- Original PDF: <source PDF filename>
  Original PDF path: <original PDF path or unresolved>
  Original PDF page: <page number or unresolved>
  Relevant passage: <short quote or tight summary anchored to that PDF page>
  Figure/image/table location: <none | original PDF page + number/caption + section + page region, with reliable coordinates when available>
  Quality: <clear/source-backed/needs-qa/gap>

Uncertainty / gaps:
- <what is not proven, conflicting, or needs QA>
```

Do not include writeback recommendations in the user-facing answer by default.
If the user explicitly asks whether to persist the result, briefly state the candidate decision and likely artifact type.
Do not include source-map, ledger, Bundle, `document.md`, table Markdown, extracted-image, or page-image paths in the user-facing answer. Record those verification carriers in the query trace. The trace path itself may still be reported as the required audit status.

## Short Locating Answer

For "where did we mention X?" style questions:

```text
Found these main hits:
- <original PDF name>, page <original PDF page>: <short context>
- <original PDF name>, page <original PDF page>: <short context>

Best starting point: <original PDF name and page>, because <reason>.
Evidence quality: <clear/source-backed/needs-qa/gap>.
```

## Engineering Evidence Answer

For engineering values, formulas, equipment composition, or code-like requirements:

```text
Conclusion: <answer or guarded answer>

Evidence chain:
- Original PDF: <source PDF filename and original PDF path>
- Original PDF page: <page number>
- Relevant passage: <short quote or tight summary anchored to that page>
- Figure/image/table location: <none | original PDF page + number/caption + section + page region, with reliable coordinates when available>

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
    original_pdf_path: <original PDF path or unresolved>
    quality: <clear | source-backed | needs-qa | gap>
existing_artifacts_checked:
  - <path or none>
qa_risks:
  - <risk or none>
```

If persisted, place candidates under `_system/reports/query-writeback-candidates/`. Later ingest must re-check source evidence and existing artifacts; it must not treat the query answer summary as source evidence.
