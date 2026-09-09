# General Knowledge Construction

Default workflow, with no business configuration or pre-enumerated systems required.
Use after source/Bundle QA and before durable knowledge writes, including synthesis and
query-derived writeback. Do not rerun conversion merely to use this workflow.

## Decisions

1. Inspect eligible source ranges. Empty or failed extraction produces a gap report, not
   facts guessed from filenames. Source headings and delivery folders are context, not proof.
2. Identify a bounded candidate set: `entity` (specific object), `concept` (abstraction),
   `requirement`, `fact`, or `analysis`. These are reasoning roles, not new storage folders
   or a domain ontology. A meaningful one-time statement can qualify; frequency is not proof.
3. Compare each candidate with existing artifacts and known names. Aliases must denote the
   same thing, not a parent class, related object or implementation. Check project, version,
   scope and object kind. Name equality alone cannot authorize merging. Preserve established
   identities; uncertain equivalence means defer or relate, not silent merge/rename.
4. Map each proposed claim to usable source text. For Bundle evidence retain its existing
   bundle/section/page/asset contract in outputs as well as the exact text ranges in the build
   record. Generated summaries frame the topic but cannot replace original evidence. Do not
   substitute candidate descriptions when supporting evidence is missing.
5. Choose `create`, `update`, `reuse`, `relate`, `defer`, or `skip`. Recognizing an entity is
   not approval to create a page. Keep existing Concept approval criteria. Specific objects
   may be described in Cards or indexes without being classified as abstract Concepts.
6. Write reader-facing knowledge, not a processing diary. Use only useful sections: subject,
   facts, applicability, limitations, differences and sources. Put classification, duplicate
   checks and rejected alternatives in the ingest log/build record. Facts stay close to
   evidence; analysis labels derivation, assumptions and coverage separately. Do not inflate
   source self-descriptions into universal claims. Never fill an empty template section by guessing.
7. Check actual links, contradictions and change impact. Navigation links do not prove claims
   or express a domain ontology. Reuse existing targets, no self-links or invented targets.
   New arrival time is not business precedence: check version, authority and applicability.
   Ambiguous conflicts remain attributed side-by-side with a review item. Source withdrawal
   triggers an affected-artifact review, not automatic deletion. Preserve valid human edits.

## Decision record and validation

For newly performed knowledge construction, save one bounded record beside the existing ingest
log: `_system/reports/<run>.knowledge-build.json`. It is an audit companion, not a new registry,
source of facts, authorization to write, or replacement for the section ledger. No record is
required retroactively for old artifacts. Only an explicitly source-preparation-only task may stop
before this stage. A request for knowledge construction requires actual source inspection, regardless
of whether new pages result. Use separate bounded records as the requested scope is processed.
Candidate IDs are local to this record; do not preallocate document/business identities here.

Minimal shape (replace example paths, ranges and fingerprint with inspected evidence):

```json
{
  "contract": "hermes-knowledge-build/v2",
  "scope": "Inspected sections and exclusions for this bounded run",
  "execution_status": "in_progress",
  "inspected_ranges": [{
    "path": "10_Raw/converted/example_document_bundle/document.md",
    "sha256": "<SHA-256 of the referenced UTF-8 text file>",
    "lines": [1, 8],
    "qa": "usable",
    "reason": "Inspected the object's stated function and project applicability",
    "ledger_path": "_system/reports/example.section-ledger.json",
    "ledger_revision": 2,
    "bundle_id": "<actual Bundle id>",
    "section_id": "<actual claimed section id>"
  }],
  "candidates": [{
    "id": "candidate-001",
    "kind": "entity",
    "name": "Object actually discussed by the source",
    "identity_rationale": "Existing targets inspected; project and scope distinguish this object",
    "existing_targets": [],
    "decision": "create",
    "reason": "Evidence supports reusable knowledge not covered by existing artifacts",
    "outputs": ["30_Cards/object.md"],
    "evidence": [{
      "path": "10_Raw/converted/example_document_bundle/document.md",
      "sha256": "<SHA-256 of the referenced UTF-8 text file>",
      "lines": [1, 8],
      "qa": "usable"
    }]
  }]
}
```

`analysis` additionally requires `derivation`. `update/reuse/relate` requires inspected existing
targets. `defer/skip` has no knowledge outputs (QA reports belong to the ingest log), and can
retain evidence marked `needs-qa`. `inspected_ranges` records actual normalized text inspected, with
fingerprint, exact inclusive lines, QA classification and a substantive finding/reason. Bundle text
also requires the ledger path, observed revision, Bundle id and section id; split noncontiguous
content_ranges into separate entries. Plain raw Markdown omits these Bundle fields. Candidate evidence
must lie within these inspected ranges. Auxiliary assets remain linked through the Bundle evidence
contract; the ranges here reference normalized text, not binary originals.

Zero candidates requires both `empty_reason` and nonempty, verifiable `inspected_ranges` explaining
the findings/exclusions. Pending ledgers, inactive versions, or "no work performed" do not establish
that content lacks useful knowledge. A wholly unreadable/missing source is a blocked preparation/QA
report, not a completed knowledge-build record. `execution_status` is `in_progress` during planning
and `completed` only after the bounded unit is handled. `blocked`/`not_started` never pass complete
validation; record blockers and remaining work in the ingest report. Do not fabricate ranges to pass.

Before knowledge writes run:

```bash
python3 "<ingest-skill-root>/scripts/validate_knowledge_build.py" "<record>" --vault "<vault>" --phase plan --require-current
```

After writing and review, finish the processed sections through the ledger manager, recording every
output under every supporting section (including multi-source outputs). Do not mark a partially read
section ingested: finish its remaining content_ranges or keep the unit incomplete with a recovery point.
Set the build record's `execution_status` to `completed` and run the same command with `--phase complete`
before declaring the knowledge unit complete. Repair/defer on failure; do not sync until provenance
registration and validation finish. Validation is read-only; it checks paths, fingerprints, ranges,
Bundle/ledger identity, terminal section state, output registration and decision consistency, not
semantic truth, actual reading, full reading coverage, actual QA quality or
the correctness of a merge. A failure requires repair/defer and must not be reported as complete.
`--phase plan` permits missing create targets only and rejects a create target that already exists;
inspect it and choose update/reuse instead. Complete records are also checked by Vault lint.

## Compatibility and impact review

Existing files and layout remain valid. Do not rewrite old cards to the new presentation format
unless the current task calls for updating them. The absence of a build record is legacy-compatible.
The validator/lint still accepts v1 records under their original structural contract; they do not
certify the v2 inspection/completion requirements. New work must use v2 and `--require-current`;
do not relabel historical empty records as v2 or backfill unobserved inspection evidence.
An existing record whose evidence hash changes is stale and fails structural validation: inspect
the cited claims and affected outputs, then record a new reviewed decision; never refresh hashes
blindly. Legacy artifacts still use source maps/ledgers for impact review. No automatic cascade
or hidden write on query/lint is introduced.
