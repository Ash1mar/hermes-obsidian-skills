# Controlled Query Fast Workflow

Use this reference for the `query_session.py` interface and evidence-query decisions.

## Performance contract

After the runtime loads the Skill, run one `bootstrap <vault-root>` call. It returns the exact applicable rule content, deployment configuration, runtime session linkage, and verification capability; do not locate them through additional searches.

Use the two-command path for one ordinary question:

```text
query (begin + automatic first-window inspect) -> finalize
```

When explicit visual verification is required, use `query --verification-required -> verify -> visual check -> finalize`. `query` automatically inspects the first three bounded candidates inside the script, `verify` is a single deterministic carrier-preparation attempt, and supplement/another inspect are disabled.

Do not scan old traces, probe stable CLI help, run inline Python, create temporary formatter scripts, or read source sections one by one. Keep progress narration sparse; the trace carries the operational detail.

The compact candidate list stays inside `query_session.py`. The script automatically inspects its first three entries, or all entries when fewer exist, and returns only evidence packets. Full fused counts and rejected candidates remain trace-only. Do not call compatibility `begin`/`inspect`, reconstruct candidates, or change selectors during a normal query. Finalize from the returned packets, using `incomplete` when necessary.

## Query and automatic inspection

```bash
python3 "<query-skill-root>/scripts/query_session.py" query \
  <vault-root> "<question>" \
  --query-type evidence
```

Optional flags:

- `--request-id`, `--question-index`, and `--question-count` for sequential multi-question requests;
- `--verification-required` only for an explicit user/audit visual-check requirement; never add it merely because of a content type, domain keyword, or Bundle QA flag;
- `--session-id` only for non-Hermes runtimes without `HERMES_SESSION_ID`;
- `--provider-config` for an explicitly deployed Provider configuration;
- `--top-sections` or `--compact-limit` only when the defaults are demonstrably insufficient.

The response contains the trace ID, up to three automatically registered evidence packets, and a minimal dynamic synthesis contract. It is serialized as one compact JSON line. Consume it directly; do not use `head`, `tail`, or another line-limiting pipe. `delivery_metrics` describes the evidence copy, while `output_metrics.stdout_chars` measures the exact complete stdout including outer response overhead and its newline. Require `output_metrics.output_complete: true`; never rerun `query`, open the trace, or read source files to recover a downstream-truncated response. The response does not expose candidate lists or the full static finalize contract. Section routing and fixed-window diversity remain deterministic inside the script. A disabled or unavailable Provider remains an attempted route but not an effective route.

## Compatibility split inspection

The following split interface is diagnostic/compatibility-only; the normal model workflow must not use it:

```bash
python3 "<query-skill-root>/scripts/query_session.py" inspect \
  <vault-root> <trace-id> --candidate 1 --candidate 4
```

Selectors may also be a section ID or `document/path::section-id`, but every form resolves only against candidates actually returned in the compact `begin` window. It never accepts a projection-only section, arbitrary path, or line range. If omitted, the command inspects the first three returned candidates. Select all useful returned candidates in this call because every later `inspect` is blocked.

Each compact evidence packet contains:

- a stable ASCII packet reference such as `P1` for final claims;

- the registered ledger-owned source-range identity and the delivered content needed for synthesis;
- original PDF identity/path and pages;
- section quality and ingest status;
- related table/image metadata, Markdown, verification image, caption, page, bbox, and QA;
- compact QA status derived from manifest, ledger, and source-map metadata;
- associated governed card/concept/project outputs;
- viewer URL when supplied by deployment metadata.

`inspect` reads and registers evidence; it never performs visual verification and never grants verified status. Follow the returned `verification_contract`. For the ordinary route, it states `verification_required: false`, requires `verified_evidence_refs: []`, and requires omission of `page-asset-verification` events.

Also follow the returned `evidence_level_contract`. Non-failed `warn`, `pending`, `qa_required`, `ambiguous`, and `incomplete` metadata, including table/image labels, appear under `non_blocking_diagnostics`; they remain directly usable as `source-backed` when substantive content and original source/pages are present. Do not read `references/evidence-levels.md` for these statuses. Cite only `usable_evidence_refs`; `excluded_evidence_refs` have packet-local blockers listed in `packet_blocked_conditions` and do not disqualify other usable refs. Actual source conflict and explicitly required incomplete visual verification remain hard blockers.

Before delivery, `inspect` applies one aggregate 30,000-character budget across the selected packets. It first removes exact duplicate asset Markdown and ordinary-route visual-audit fields, then—only when still over budget—keeps query-matched Markdown blocks and adjacent context. A packet or asset marked `delivery_excerpted` is a shortened agent copy; it is not a failed source read and must not be confused with `content_truncated`. `delivery_metrics` and the diagnostic `evidence-packet-delivery` event record `full_packet_chars`, `agent_packet_chars`, saved characters, excerpted fields, and whether the evidence budget was satisfied. The combined command separately records total compact stdout and response overhead in `output_metrics`; the evidence budget is not falsely presented as a total-output limit. Do not open the full trace or source to reverse delivery optimization.

The full provenance catalog and complete source ranges remain in the trace sidecar and are inherited by packet reference; the underlying registered source remains reconstructible from those ranges. The packet is navigation and verification material, not a user-facing citation.

When `query` was explicitly given `--verification-required`, prepare each cited registered visual carrier exactly once:

```bash
python3 "<query-skill-root>/scripts/query_session.py" verify \
  <vault-root> <trace-id> --evidence-ref P1
```

- `ready`: open the returned image/viewer once, then submit a completed `page-asset-verification` event.
- `unavailable` or `failed`: stop; use `needs-qa` and preserve `required_unresolved`.

Do not probe `pdftotext`, Python PDF packages, alternative binaries, Bundle listings, or converted text. Those paths cannot replace visual original-page verification.

## Finalize

Prefer `--decision-stdin` so JSON bypasses shell argument quoting. For ordinary-minimal mode, send only `claims` and `conclusion`; do not write a temporary manifest or decision file:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <trace-id> --decision-stdin <<'JSON'
{"claims":[{"text":"Concise final claim.","evidence_refs":["P1"]}],"conclusion":"Short supported conclusion."}
JSON
```

Keep the conclusion within the scope supported by inspected evidence. Express a narrower scope as a short evidence-derived qualification without launching additional retrieval merely to make the answer broader. This is a general synthesis rule, not a domain-specific routing or answer template.

Use the minimum sufficient claim set: each claim must answer a requested output attribute or action, while subject qualifiers only narrow its scope. Closely related parameters supported by the same evidence should be merged. Put background, comparison, applicability, operational detail, scope, or evidence limits into a brief qualification on the affected claim instead of creating another claim, unless the question explicitly requests that material. Record only unresolved items that materially affect correctness or use, and write one short conclusion without restating each claim. Submit the smallest valid decision object; do not copy packet prose or provenance into it.

Ordinary-minimal decision shape:

```json
{
  "claims": [
    {
      "text": "Concise final claim.",
      "evidence_refs": ["P1"]
    }
  ],
  "conclusion": "Short supported conclusion."
}
```

The script supplies the omitted ordinary defaults. Only when `query --verification-required` selected the visual route, `verify` returned `ready`, and the registered carrier was actually viewed may `verified_evidence_refs` contain packet refs; each such ref then requires a completed `page-asset-verification` event with non-empty `inspected_paths`.

Before using those defaults, apply the returned `semantic_sufficiency_gate`: visible packet validity is not proof that every requested output was answered. When any requested output lacks support, use the returned `incomplete_fallback` without reading another reference. Set `status: incomplete`, use `source-backed` if supported requested claims remain and otherwise `gap`, keep only those supported claims, and provide at least one material string in `unresolved`. The script defaults verification refs and events to empty on this ordinary incomplete route and renders the conclusion plus unresolved boundary even when there are no supported claims.

Follow `event_submission_contract` as well. For an ordinary query, submit `events: []`; query-session already records candidate selection, inspect, reading, assets, and provenance. Do not add a claim, comparison, or evidence ref merely to make an optional event reference count as used.

The script expands each packet reference into path, document version, section, pages, source PDF and viewer metadata, then assigns `E1...` and `C1...`. Every claim requires non-empty text; `claim`, `statement`, and `claim_text` are accepted as aliases for `text`. Supported claims require at least one recorded evidence ID, derived from an inspected packet reference. Use `qualified`, `disputed`, or `gap` when appropriate.

Immediately before finalize, apply `claim_pruning_gate`: remove every claim whose deletion still leaves all requested outputs answered. Evidence availability does not create answer scope. Unrequested comparison, background, applicability, and operational material belongs only in a necessary qualification on a requested claim; otherwise omit it.

Finalization accepts only the documented top-level decision and claim fields. `unresolved_items` is a compatibility alias for `unresolved`; other unknown top-level or claim fields are rejected. Event standard fields are `stage`, `route`, `status`, `summary`, `evidence_refs`, `inspected_paths`, `hit_count`, `duration_ms`, and `accounting`. Unknown event fields such as a model-supplied `type` are preserved under `extensions`; they are diagnostic metadata only and cannot satisfy stage, evidence, or verification gates. Events are optional unless an actual visual verification must be recorded. A verified reference requires a completed `page-asset-verification` event with non-empty `inspected_paths`. When the trace marks verification as required, unverified evidence cannot be `clear` or `source-backed`; `needs-qa` also requires a non-empty unresolved item. An invalid or uninspected reference leaves the existing trace in progress without partially adding final evidence or claims. Legacy manifest inputs remain compatibility/debugging interfaces only.

## Single-pass boundary

Supplemental retrieval is disabled. The retained `supplement` CLI is a compatibility guard: it performs no retrieval and returns `blocked` with `next_command: finalize`. A second `inspect` is also blocked. If the single compact-window inspection leaves material evidence missing, finalize as `incomplete` and state that specific boundary in `unresolved`; do not let answer synthesis turn into another search phase.

For multiple questions, pass the same expected count to every `query`. On the final trace, close and render compact capsules in the same command:

```bash
python3 "<query-skill-root>/scripts/query_session.py" finalize \
  <vault-root> <last-trace-id> --decision-json '<json-object>' --close-request
```

For later inspection/debugging only, rerender with:

```bash
python3 "<query-skill-root>/scripts/query_session.py" request-summary \
  <vault-root> <request-id>
```

`query` rejects two or more question marks or numbered question items before trace creation. It also rejects a new trace while the same request has an open trace, duplicate/gapped question indices, and inconsistent expected counts. `--close-request` rejects unfinished or incomplete request groups. Split independently answerable questions under one request ID. Only genuinely coupled subparts may bypass the question-shape guard with `--coupled --coupled-reason "<shared evidence reason>"`.

## Search decisions

Use the fused candidate union as scope, not as evidence. Prefer selected governed card/concept/project content when it provides current, direct provenance. Otherwise use the complete source section supplied by `inspect`.

Do not run supplemental lexical search. Treat the automatically inspected compact window as the retrieval boundary for this trace, including gap, completeness, audit, conflict, and version-applicability questions. When it cannot resolve a necessary point, preserve it as a material unresolved item and finalize as `incomplete`. A disabled Provider does not change this boundary; hierarchical candidates remain the normal fallback used by `query`.

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
