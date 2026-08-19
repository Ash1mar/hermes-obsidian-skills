# Controlled Query Fast Workflow

Use this reference for the `query_session.py` interface and evidence-query decisions.

## Performance contract

After the runtime loads the Skill, run one `bootstrap <vault-root>` call. It returns the exact applicable rule content, deployment configuration, runtime session linkage, and verification capability; do not locate them through additional searches.

Use the three-command path for one ordinary question:

```text
begin -> inspect -> finalize
```

When the caller explicitly requires visual source verification, use `begin --verification-required -> inspect -> verify -> visual check -> finalize`. `verify` is a single deterministic carrier-preparation attempt. The script never infers this policy from question wording. Run a second `inspect` only for a real evidence gap or conflict.

Do not scan old traces, probe stable CLI help, run inline Python, create temporary formatter scripts, or read source sections one by one. Keep progress narration sparse; the trace carries the operational detail.

## Begin

```bash
python3 "<query-skill-root>/scripts/query_session.py" begin \
  <vault-root> "<question>" \
  --query-type evidence
```

Optional flags:

- `--request-id`, `--question-index`, and `--question-count` for sequential multi-question requests;
- `--verification-required` when the evidence requirement calls for a visual source check; never add it merely because a domain keyword appears;
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

Each compact evidence packet contains:

- a stable ASCII packet reference such as `P1` for final claims;

- the complete ledger-owned source ranges;
- original PDF identity/path and pages;
- section quality and ingest status;
- related table/image metadata, Markdown, verification image, caption, page, bbox, and QA;
- compact QA status derived from manifest, ledger, and source-map metadata;
- associated governed card/concept/project outputs;
- viewer URL when supplied by deployment metadata.

The full provenance catalog remains in the trace sidecar and is inherited by packet reference. The packet is navigation and verification material, not a user-facing citation.

When `begin` was explicitly given `--verification-required`, prepare each cited registered visual carrier exactly once:

```bash
python3 "<query-skill-root>/scripts/query_session.py" verify \
  <vault-root> <trace-id> --evidence-ref P1
```

- `ready`: open the returned image/viewer once, then submit a completed `page-asset-verification` event.
- `unavailable` or `failed`: stop; use `needs-qa` and preserve `required_unresolved`.

Do not probe `pdftotext`, Python PDF packages, alternative binaries, Bundle listings, or converted text. Those paths cannot replace visual original-page verification.

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

The script expands each packet reference into path, document version, section, pages, source PDF and viewer metadata, then assigns `E1...` and `C1...`. Every claim requires non-empty text; `claim`, `statement`, and `claim_text` are accepted as aliases for `text`. Supported claims require at least one recorded evidence ID, derived from an inspected packet reference. Use `qualified`, `disputed`, or `gap` when appropriate.

Finalization accepts only the documented decision fields. `unresolved_items` is a compatibility alias for `unresolved`; other unknown fields are rejected. A verified reference requires a completed `page-asset-verification` event with non-empty `inspected_paths`. When the trace marks verification as required, unverified evidence cannot be `clear` or `source-backed`; `needs-qa` also requires a non-empty unresolved item. An invalid or uninspected reference leaves the existing trace in progress without partially adding final evidence or claims. Legacy manifest inputs remain compatibility/debugging interfaces only.

## Supplemental retrieval

When a real evidence gap remains, record it and retrieve a focused scope:

```bash
python3 "<query-skill-root>/scripts/query_session.py" supplement \
  <vault-root> <trace-id> "<focused query>" --reason "<gap>"
```

Then run `inspect` on the supplemental candidate. `finalize` rejects a trace with pending supplemental evidence, so supplemental search cannot be hidden inside answer synthesis.

For multiple questions, pass the same expected count to every begin. On the final trace, close and render compact capsules in the same command:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <last-trace-id> --decision-json '<json-object>' --close-request
```

For later inspection/debugging only, rerender with:

```bash
python3 "<query-skill-root>/scripts/query_session.py" request-summary \
  <vault-root> <request-id>
```

`begin` rejects two or more question marks or numbered question items before trace creation. It also rejects a new trace while the same request has an open trace, duplicate/gapped question indices, and inconsistent expected counts. `--close-request` rejects unfinished or incomplete request groups. Split independently answerable questions under one request ID. Only genuinely coupled subparts may bypass the question-shape guard with `--coupled --coupled-reason "<shared evidence reason>"`.

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
