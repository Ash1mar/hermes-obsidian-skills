---
name: hermes-obsidian-knowledge-finalize
description: Incrementally finalize completed Hermes knowledge builds into an auditable Vault release and explicitly project that committed release to the retrieval Provider. Use after Build Finalize to analyze changed or withdrawn SourceUnit contributions, validate aliases and wikilinks, create safe redirects, publish navigation and eligibility, validate/recover a release, or synchronize its P5 index generation. Does not parse sources or approve business versions.
---

# Hermes Obsidian Knowledge Finalize

Use this Skill only when `_system/vault.json` declares phase `P5` and
`capabilities.vault_finalize: true`. Read `references/vault-finalize.md` before
planning a release.

Run the entry point explicitly:

```bash
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" <vault-root> plan --request <request.json>
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" <vault-root> apply --request <request.json>
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" <vault-root> validate --release-id <release-id>
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" <vault-root> status
```

`plan` is read/analysis plus a persisted immutable proposal. Review its affected
subjects, stale/withdrawn contributions, redirects, link blockers and index
eligibility before `apply`. `apply` requires the exact `plan_id`, owner, and plan
revision. It refuses unresolved navigation blockers and rechecks all pinned inputs.

Never edit a plan or release manifest by hand. Fix the underlying page, identity,
source state, or Build Finalize result and create a new release ID. A completed
release may be revalidated but is immutable.

Vault Finalize itself never calls qmd-like-rag, Chroma, BM25, QMD or an HTTP Provider. Its
index-eligibility list is an auditable P5 input. `business-unassessed`,
`visibility-draft`, QA restrictions, stale contributions and withdrawn sources
remain explicit ineligibility reasons.

## Release-driven retrieval projection

After `apply` commits the release, optionally run:

```bash
python3 "<finalize-skill-root>/scripts/sync_release_index.py" <vault-root>
```

This is the only Skill-side command that may update the P5 coarse-recall Provider. It reads
the current release ID and exact manifest hash, submits both through command or HTTP transport,
and atomically writes `_system/reports/retrieval-index-manifest.json`. The Provider must expose
the `source_units` and `release_driven` capabilities, use the configured model tokenizer, and
create a separate index generation. A disabled or failed adapter leaves the committed release
valid but keeps `query_ready` false. Never run this command before `apply`, from Query, or after
an uncommitted plan. Read `references/release-indexing.md` for deployment and rebuild rules.
