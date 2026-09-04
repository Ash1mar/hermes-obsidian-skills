# Vault Profiles

## General

Use for general knowledge vaults.

Standard folders:

```text
00_Inbox/
10_Raw/
10_Raw/converted/
20_Notes/
30_Cards/
40_Concepts/
50_Projects/
90_Dataview/
_system/metadata/
_system/prompts/
_system/reports/
_system/templates/
```

Default routing:

- `10_Raw/`: read-only original sources
- `20_Notes/`: structured notes
- `30_Cards/`: reusable knowledge cards
- `40_Concepts/`: stable concept pages
- `50_Projects/`: project notes
- `_system/reports/`: setup and ingest logs

## Meeting

Use for meeting-minutes vaults.

Additional folders:

```text
10_Raw/Meetings/
10_Raw/Materials/
20_Notes/Meetings/
```

Meeting-specific routing:

- `10_Raw/Meetings/`: original meeting minutes, usually named `YYYY-MM-DD Topic.md`
- `10_Raw/Materials/`: meeting-related background files
- `10_Raw/converted/`: converted Markdown from non-Markdown sources
- `20_Notes/Meetings/`: structured meeting notes generated from raw minutes
- `50_Projects/`: recurring projects or workstreams discovered from meetings
- `30_Cards/`: reusable knowledge that survives outside one meeting

Meeting note metadata:

```yaml
---
type: meeting-note
status: draft
created:
meeting_date:
topic:
source:
related_materials:
participants:
projects:
domains:
---
```

## Engineering

Use for multi-organization engineering or regulated-document Vaults that need stable document identity,
business-version governance, authority state, and future storage/database portability.

It uses the standard folders and additionally creates:

```text
_system/vault.json
_system/metadata/document-governance.schema.json
_system/metadata/source-organizations.json
_system/metadata/document-registry.json
```

The registries start empty at revision 0 and the Vault starts with `governance.readiness: draft`.
`repository.backend: json` is the current authority; SQLite and PostgreSQL are planned adapters, not
active stores. See `governance-control-plane.md` for identity and maintenance rules.
