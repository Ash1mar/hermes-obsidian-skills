# Controlled Query Fast Workflow

Use this reference for the `query_session.py` interface and evidence-query decisions.

## Performance contract

Use the three-command path for one ordinary question:

```text
begin -> inspect -> finalize
```

Allow one additional original-page visual tool call for tables, figures, formulas, or disputed extraction. Run a second `inspect` only for a real evidence gap or conflict.

Do not scan old traces, probe stable CLI help, run inline Python, create temporary formatter scripts, or read source sections one by one. Keep progress narration sparse; the trace carries the operational detail.

## Begin

```bash
python3 "<query-skill-root>/scripts/query_session.py" begin \
  <vault-root> "<question>" \
  --query-type evidence
```

Optional flags:

- `--request-id` and `--question-index` for sequential multi-question requests;
- `--session-id` only for non-Hermes runtimes without `HERMES_SESSION_ID`;
- `--provider-config` for an explicitly deployed Provider configuration;
- `--top-sections` or `--compact-limit` only when the defaults are demonstrably insufficient.

The response contains the trace ID and at most five compact fused candidates. A disabled or unavailable Provider remains an attempted route but not an effective route.

## Inspect

Select by one-based fused rank:

```bash
python3 "<query-skill-root>/scripts/query_session.py" inspect \
  <vault-root> <trace-id> --candidate 1 --candidate 4
```

Selectors may also be a section ID or `document/path::section-id`. If omitted, the command inspects the first three candidates; explicit selection is preferred.

Each evidence packet contains:

- a stable ASCII packet reference such as `P1` for final claims;

- the complete ledger-owned source ranges;
- original PDF identity/path, pages, and source hash when present;
- section quality and ingest status;
- related table/image metadata, Markdown, verification image, caption, page, bbox, and QA;
- manifest, ledger, source-map, and source-state pointers;
- associated governed card/concept/project outputs;
- viewer URL when supplied by deployment metadata.

The packet is navigation and verification material, not a user-facing citation. For a parameter, formula, table row, or figure internal, open the returned original PDF/page image or evidence image and check the precise region before finalizing.

## Finalize

Use `--decision-json`. Do not write a temporary manifest:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <trace-id> --decision-json '<json-object>'
```

Decision shape:

```json
{
  "status": "completed",
  "evidence_level": "clear",
  "claims": [
    {
      "text": "Concise final claim.",
      "status": "supported",
      "evidence_refs": ["P1"],
      "qualification": null
    }
  ],
  "verified_evidence_refs": ["P1"],
  "events": [
    {
      "stage": "page-asset-verification",
      "route": "original-pdf",
      "status": "completed",
      "summary": "Checked table 5.1.1-1 on PDF page 18.",
      "evidence_refs": ["P1"],
      "inspected_paths": ["10_Raw/example.pdf"]
    }
  ],
  "conclusion": "Short supported conclusion.",
  "unresolved": []
}
```

The script expands each packet reference into path, document version, section, pages, source PDF and viewer metadata, then assigns `E1...` and `C1...`. Supported claims require at least one recorded evidence ID, derived from an inspected packet reference. Use `qualified`, `disputed`, or `gap` when appropriate.

Finalization validates the entire payload before writing it. An invalid or uninspected reference leaves the existing trace in progress without partially adding final evidence or claims. Legacy manifest inputs remain compatibility/debugging interfaces only.

## Supplemental retrieval

When a real evidence gap remains, record it and retrieve a focused scope:

```bash
python3 "<query-skill-root>/scripts/query_session.py" supplement \
  <vault-root> <trace-id> "<focused query>" --reason "<gap>"
```

Then run `inspect` on the supplemental candidate. `finalize` rejects a trace with pending supplemental evidence, so supplemental search cannot be hidden inside answer synthesis.

For multiple questions, finish each trace and then render compact capsules with:

```bash
python3 "<query-skill-root>/scripts/query_session.py" request-summary \
  <vault-root> <request-id>
```

## Search decisions

Use the fused candidate union as scope, not as evidence. Prefer selected governed card/concept/project content when it provides current, direct provenance. Otherwise use the complete source section supplied by `inspect`.

Run supplemental lexical search only when:

- a precise identifier or phrase is missing from the packet;
- the selected candidate does not answer a necessary subpart;
- a gap, completeness, or audit question requires widening;
- sources conflict or version applicability is unresolved.

Do not broaden merely because the Provider is disabled. Hierarchical candidates remain the normal fallback.

## Engineering evidence

Treat design values, formulas, pressures, flows, intensities, areas, classifications, tables, and figures as evidence queries. Preserve:

- original PDF and page;
- project/document applicability;
- table or figure caption and region;
- extraction quality and cross-page warnings;
- conflicting values from other projects or revisions.

A pass-quality converted section can support textual claims. Formula semantics, table rows, figure internals, and warning-affected engineering parameters require targeted original-page or evidence-image review.

## Failure fallback

If the session entry point itself fails, use the legacy scripts separately and record the fallback reason. Do not revert to inline Python or temporary helpers. Retrieval or trace logging failure must not mutate the Provider or governed artifacts.
