# P2 Source-unit operations

Use this path only when `_system/vault.json` declares `phase: P2` and
`capabilities.source_reader: true`. It is the new source-preparation path; it does
not run knowledge construction, Finalize, Provider sync or query.

The runtime is embedded under the Skill `lib/` directory and uses Python 3.11+
standard library only. All paths passed to the commands below are Vault-relative.

## Prepare a normalized artifact

For governed Bundle v2 after `ingest-finish` has added `manifest.governance`:

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  prepare-bundle --bundle "10_Raw/converted/example_bundle"
```

For immutable UTF-8 Markdown already under the Vault, supply the identity already
registered for the source:

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  prepare-markdown --source "10_Raw/example.md" \
  --document-id doc-example --version-id version-example-1 --resource-id resource-example-1
```

Preparation normalizes CRLF/CR to LF, generates or imports an outline, copies
declared Bundle assets into an immutable artifact revision, hashes every stored
component and returns the artifact-manifest path. It does not publish units.

## Preview and publish

Preview and build use identical generation. Read the resource's current pointer;
use revision `0` when none exists. A mismatch is a conflict, not permission to
overwrite.

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  preview --artifact-manifest "_system/sources/artifacts/<resource>/<revision>/manifest.json" \
  --actor "<actor>" --expected-revision 0

python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  build --artifact-manifest "_system/sources/artifacts/<resource>/<revision>/manifest.json" \
  --actor "<actor>" --expected-revision 0
```

Build publishes `manifest.json`, `sections.json`, `units.jsonl` and diagnostics
under `_system/sources/units/<resource>/<unit-set>/`, then atomically updates
`current.json`. Published revision directories are immutable. Repeating the same
build is idempotent; an historical unit set cannot silently become current again.

## Read and validate

`list` and `validate` accept a resource and optional unit-set ID. `get` reads a
JSON SourceRef; `context` reads a JSON array of SourceRefs. Reads require the
current document-registry revision and recheck artifact/unit hashes. Context expands
within the unit's section and its ancestor sections under a codepoint budget. It
does not enter sibling child sections, and returned context never counts as
inspected core evidence.

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" \
  validate --resource-id resource-example-1
```

Text coordinates are LF-normalized Unicode codepoint, zero-based half-open spans.
Source units are the canonical chunks shared by knowledge construction and the
future Provider. They use the source overlap configuration without crossing a
section owned range. Heading breadcrumbs and context are separate.
The packaged starting values are 512 target, 1024 maximum and 80 overlap
codepoints. Changing strategy, size or overlap produces a new UnitSet; Provider
model or rendering changes do not.
Oversized fenced code, formulas and tables are preserved and reported rather than
silently truncated. Unknown pages or image regions remain unknown.

P2 completion means normalized artifacts and source units are usable. Do not
create knowledge-build v4 records or replace Provider input until P3/P5 ships.
