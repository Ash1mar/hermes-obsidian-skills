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
    └── skills/
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
- `_system/skills/`: local skill drafts or vault-specific skill notes.

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

## Bundle Source Map and Section Ledger

For Bundle v2, generate these paired control files under `_system/reports/`:

```text
<source-stem>.source-map.md
<source-stem>.section-ledger.json
```

Create and refresh them with `scripts/manage_bundle_ingest.py`; do not hand-copy the generic report template. The Markdown source map is the human-readable view, while the JSON ledger is the authority for section status, revisions, hashes, outputs, and resumption.

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
