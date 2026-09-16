---
name: hermes-obsidian-knowledge-finalize
description: Incrementally finalize completed Hermes knowledge builds into an auditable Vault release. Use after P3 Build Finalize to analyze changed or withdrawn SourceUnit contributions, validate aliases and wikilinks, create safe redirects for moved pages, publish navigation and index-eligibility projections, or validate/recover a P4 release. Does not parse sources, approve business versions, or synchronize a retrieval Provider.
---

# Hermes Obsidian Knowledge Finalize

Use this Skill only when `_system/vault.json` declares phase `P4` and
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

This Skill never calls qmd-like-rag, Chroma, BM25, QMD or an HTTP Provider. Its
index-eligibility list is an auditable P5 input. `business-unassessed`,
`visibility-draft`, QA restrictions, stale contributions and withdrawn sources
remain explicit ineligibility reasons.
