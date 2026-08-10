# Hierarchical Search: Phase 1 Design

- Change origin: **hanyu**
- Scope: first-phase integration of hierarchical source navigation into Hermes Obsidian controlled ingest/query
- Authority: design and operational documentation; query projections remain non-authoritative

## Goal

Add hierarchical source retrieval without replacing the existing governed query workflow or changing existing ingest artifact schemas.

The overall workflow remains sequential, while source-candidate recall becomes locally parallel:

```text
question
-> optional coarse recall || hierarchical document / section locator
-> normalize to complete sections, union, deduplicate, and RRF-rank candidates
-> governed-layer-first traditional search
-> supplemental scoped exact/lexical search when needed
-> resolve source-map / ledger metadata
-> verify document.md, tables, figures, and original pages
-> apply existing evidence levels and answer contract
```

The hierarchical locator returns only candidate documents and sections. It does not answer the question, replace evidence, or write knowledge artifacts.

## Components Added

### Ingest-side query projection

`python3 "<ingest-skill-root>/scripts/build_section_query_index.py"` reads existing Bundle v2 and control artifacts and writes a per-source projection under:

```text
_system/reports/query-index/<source>.section-query-index.json
```

The projection is explicitly:

- non-authoritative;
- rebuildable and disposable;
- additive to the existing ingest workflow;
- non-blocking when generation fails;
- free of generated section summaries.

It reuses existing fields such as document path, section ID, title, parent/path hierarchy, owned `content_ranges`, pages, assets, quality, ingest status, and content hash. It also exposes lexical routing terms derived from ingest-maintained source names, Bundle metadata, optional manifest routing metadata, and spec-index paths. It does not encode nuclear-domain relationships in the Skill or script.

Existing artifacts are not modified:

- `manifest.json`
- `outline.json`
- `document.md`
- `tables/`, `images/`, `_evidence/`
- section ledger
- source map
- spec index
- ingest log
- governed cards, concepts, and projects

### Query-side candidate locator

`python3 "<query-skill-root>/scripts/locate_source_sections.py"` reads the projections, scores document routing and section title/path matches, and dynamically scans the authoritative `document.md` owned ranges. It emits candidate JSON containing paths, document and section IDs, `match_start_line` / `match_end_line`, pages, quality/status, matched terms, and scores. On the intranet branch it also composes a deployment-local `viewer_url` from those four positioning fields and `config/intranet.json`; this URL remains a navigation aid rather than evidence.

The controlled-query workflow merges these hits with optional Provider candidates through `retrieve_query_scope.py`. It expands Provider chunks to complete projected/ledger-owned sections, takes the union, merges duplicate document/section or overlapping same-title ranges, preserves route scores/ranks, and records RRF ordering plus rejection reasons. It then performs governed-layer-first traditional search and verifies original converted source and page/table/figure evidence. If no projection is present, the hierarchical route is recorded as empty/unavailable and traditional fallback continues unchanged.

## Routing Knowledge Ownership

The original practice contained hard-coded FNP/FEP/FSP/FDP, building, project, and document-choice knowledge. Phase 1 does not move those claims into the controlled-query Skill.

Routing knowledge is owned by ingest and governed Vault artifacts:

- source-local routing comes from the source filename, manifest, spec index, and optional ingest-maintained manifest routing metadata;
- cross-source system responsibility or applicability remains a governed ingest conclusion with evidence;
- the query layer only implements the generic method for consuming routing metadata.

This prevents historical domain assumptions from becoming permanent retrieval code.

## Summary Decision

Phase 1 does not create or require section summaries. Candidate location uses:

1. document/source routing terms;
2. section titles;
3. full parent-title paths;
4. dynamic scanning of ledger-owned source ranges.

This avoids LLM calls, summary hallucination, summary freshness state, and changes to ingest completion. A future retrieval enhancement may add disposable summaries, but they are outside this phase and must never become evidence.

## Reuse From `otherCode/nuclear-doc-query`

The following ideas were retained and generalized:

- coarse-to-fine navigation: document domain -> document -> heading tree -> complete section -> page evidence;
- prefer heading hierarchy before broad full-text reading;
- read complete semantic/owned ranges instead of a fixed small line window;
- search multiple relevant documents for cross-project questions;
- preserve page/line provenance;
- use content scanning after heading navigation because engineering parameters often do not appear in headings;
- degrade to the existing search route when the hierarchical index is unavailable.

## Content Not Reused

The following implementation details were intentionally not carried forward:

- fixed `/opt/data/mvp/` paths;
- the standalone `index_title_with_summary_with_page.json` schema;
- generated chapter summaries as a required ingest dependency;
- the standalone page-number lookup script and its substring-only first-match behavior;
- hard-coded nuclear-domain routing claims and project/system responsibility tables;
- shell/tool restrictions specific to the original runtime;
- a second, independent answer pipeline;
- direct use of index content as answer evidence;
- vector retrieval, BM25, reranking, or other later-phase retrieval methods.

The main branch accepts an explicit Vault root. The intranet branch continues to resolve its fixed Vault root from `config/intranet.json`, currently `/opt/data/phq/testVault`, and passes that root to the same scripts.

## Operation

Build or validate projections without touching existing artifacts:

```bash
python3 "<ingest-skill-root>/scripts/build_section_query_index.py" \
  /path/to/vault --check
```

Build projections:

```bash
python3 "<ingest-skill-root>/scripts/build_section_query_index.py" \
  /path/to/vault
```

Locate candidates:

```bash
python3 "<query-skill-root>/scripts/locate_source_sections.py" \
  /path/to/vault "query text"
```

Resolve `<query-skill-root>` from the location of the active `SKILL.md` supplied by the runtime's Skill loader. Do not hard-code or guess an installation directory, and never resolve it inside the Vault. The build step should run after ledger initialization/reconciliation or completed source ingest. Its failure is a warning only. The query step must always re-open the authoritative source before using any result.
