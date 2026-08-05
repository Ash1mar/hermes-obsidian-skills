# Vault Structure

Use this layout for governed Hermes + Obsidian ingestion:

```text
Vault/
├── 00_Inbox/
├── 10_Raw/
│   └── converted/
├── 20_Notes/
├── 30_Cards/
├── 40_Concepts/
├── 50_Projects/
├── 90_Dataview/
└── _system/
    ├── metadata/
    ├── prompts/
    ├── reports/
    └── templates/
```

## Folder Roles

- `10_Raw/`: raw source files and converted Markdown. Read-only after creation.
- `20_Notes/`: human-oriented notes that are not reusable cards.
- `30_Cards/`: concise reusable knowledge cards.
- `40_Concepts/`: stable concept pages only.
- `50_Projects/`: project packages and implementation plans.
- `90_Dataview/`: Dataview dashboards and view notes.
- `_system/metadata/`: registries and governance metadata.
- `_system/prompts/`: reusable prompts.
- `_system/reports/`: ingest logs, source maps, reviews, batch plans.
- `_system/reports/query-writeback-candidates/`: optional query-derived writeback queue. These files are triage records, not knowledge artifacts.
- `_system/templates/`: Vault content templates, not runtime Skill files or executable scripts.

## Knowledge Card Template

```markdown
---
type: knowledge-card
source:
status: draft
created:
domains:
---

# Title

## 来源范围
## 一句话摘要
## 材料性质判断
## 核心观点
## 可复用方法
## 与已有知识库的关系
## 与已有卡片的重复性检查
## 适用场景
## 限制与风险
## 场景 / 对象 / 行为 / 方法 / 规则
## 关联概念
## 候选概念但不建页
## 来源
```

## Concept Page Template

```markdown
---
type: concept
source:
status: draft
created:
domains:
---

# Concept Name

## 定义
## 为什么重要
## 与来源材料的关系
## 和已有概念的区别
## 适用边界
## 不应包含的内容
## 关联卡片
## 来源
```

## Project Note Template

```markdown
---
type: project-note
source:
status: draft
created:
domains:
---

# Project Title

## 项目性质判断
## 源文件分工
## 核心目标
## 系统对象
## 关键流程
## AI 介入点
## 与现有知识库的关系
## 可复用方法
## 项目风险与不确定性
## 后续可拆分产物
## 来源
```

## Spec Index Template

```markdown
---
type: spec-index
source:
status: draft
created:
domains:
---

# Spec Index Title

## 材料性质判断
## 核心用途
## 结构摘要
## 关键字段或对象
## 使用规则
## 与现有知识库的关系
## 可复用部分
## 不应过度沉淀的部分
## 是否需要概念页
## 后续处理建议
## 来源
```

## Ingest Log Template

```markdown
---
type: report
source:
status: draft
created:
domains:
---

# Ingest Log

## 本次任务
## 输入源
## 前置读取
## 材料性质判断
## 创建文件
## 未创建文件及原因
## 与已有概念的关系
## 是否创建概念页及理由
## 10_Raw 只读规则
## 后续建议
```

## Query Writeback Candidate Template

Use this only when query logging is explicitly allowed by the user or vault policy. It is a handoff for later controlled ingest, not a source of truth.

```markdown
---
type: query-writeback-candidate
status: candidate
created:
domains:
---

# Query Writeback Candidate

## User Question
## Answer Summary
## Candidate Type
## Evidence Level
## Possible Artifact
## Why Candidate
## Why Not Direct Write
## Evidence Packets
## Existing Artifacts Checked
## QA Risks
## Later Ingest Decision
```

Later ingest must re-check the cited source evidence and existing artifacts before writing any card, concept, spec index, project note, or QA item.

## Conservative Wikilink Policy

Use Obsidian `[[wikilinks]]` for navigation between existing governed artifacts, not for raw evidence or speculative graph density.

For every new or reconciled knowledge card, include:

```yaml
evidence_mode: direct | index | relational
```

An index or cross-source synthesis card also includes:

```yaml
evidence_scope: multi-source
evidence_coverage: complete | representative
evidence_authority: navigation
source_reports:
  - "_system/reports/<source>.source-map.md"
  - "_system/reports/<source>.section-ledger.json"
```

Use concrete paths when the source set is bounded. A glob is acceptable only for a batch-wide index whose body contains a structured row-level evidence table. When `evidence_coverage` is `representative`, state that explicitly next to the table and identify where the complete set is governed. Never encode omitted evidence as a table row containing only `…`.

Good wikilink targets:

- existing `30_Cards/` files
- existing `40_Concepts/` files
- existing `50_Projects/` files
- relevant spec indexes, source maps, candidate reviews, and maintained Dataview notes

Avoid wikilinking:

- raw PDFs or converted bundle files
- every repeated noun, equipment name, field name, section heading, or parameter
- concepts that have not passed concept governance
- evidence assets such as table/image files; cite these as source paths instead

If a target would be useful but does not exist, record it as a possible future card, candidate concept, or QA/review item rather than creating an empty link.

After a source or batch has created all artifacts, revisit the completed set and add typed links among existing governed knowledge. Deferring a Concept is not a reason to omit Card-to-Card or Card-to-index relationships.

## Bundle Source Map and Section Ledger

For Bundle v2, generate these paired control files under `_system/reports/`:

```text
<source-stem>.source-map.md
<source-stem>.section-ledger.json
```

Create and refresh them with `python3 "<ingest-skill-root>/scripts/manage_bundle_ingest.py"`; do not hand-copy the generic report template. The Markdown source map is the human-readable view, while the JSON ledger is the authority for section status, revisions, hashes, outputs, and resumption.

Every governed artifact created from a Bundle section must include:

```yaml
source_bundle_id:
source_sha256:
source_section_id:
source_lines:
source_pages: []
source_assets: []
```

See `references/bundle-source-map-ledger.md` for lifecycle and recovery rules.
