#!/usr/bin/env python
"""Initialize a governed Obsidian vault for Hermes workflows."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path


BASE_DIRS = [
    "00_Inbox",
    "10_Raw",
    "10_Raw/converted",
    "20_Notes",
    "30_Cards",
    "40_Concepts",
    "50_Projects",
    "90_Dataview",
    "_system/metadata",
    "_system/prompts",
    "_system/reports",
    "_system/skills",
    "_system/templates",
]

PROFILE_DIRS = {
    "general": [],
    "meeting": ["10_Raw/Meetings", "10_Raw/Materials", "20_Notes/Meetings"],
}


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def is_non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def normalize_workspace(vault: Path, profile: str) -> None:
    obsidian = vault / ".obsidian"
    obsidian.mkdir(parents=True, exist_ok=True)
    workspace = {
        "main": {
            "id": "hermes-vault-main",
            "type": "split",
            "children": [
                {
                    "id": "hermes-vault-tabs",
                    "type": "tabs",
                    "children": [
                        {
                            "id": "hermes-dashboard",
                            "type": "leaf",
                            "state": {
                                "type": "markdown",
                                "state": {
                                    "file": "90_Dataview/00_Dashboard.md",
                                    "mode": "source",
                                    "source": False,
                                },
                                "icon": "lucide-file",
                                "title": "Dashboard",
                            },
                        }
                    ],
                }
            ],
            "direction": "vertical",
        },
        "left": {
            "id": "hermes-vault-left",
            "type": "split",
            "children": [
                {
                    "id": "hermes-vault-left-tabs",
                    "type": "tabs",
                    "children": [
                        {
                            "id": "hermes-file-explorer",
                            "type": "leaf",
                            "state": {
                                "type": "file-explorer",
                                "state": {"sortOrder": "alphabetical", "autoReveal": False},
                                "icon": "lucide-folder-closed",
                                "title": "Files",
                            },
                        }
                    ],
                }
            ],
            "direction": "horizontal",
            "width": 300,
        },
        "right": {
            "id": "hermes-vault-right",
            "type": "split",
            "children": [],
            "direction": "horizontal",
            "width": 300,
            "collapsed": True,
        },
        "active": "hermes-dashboard",
        "lastOpenFiles": [
            "90_Dataview/00_Dashboard.md",
            "_system/prompts/hermes-ingest-rules.md",
            "README.md",
        ],
    }
    if profile == "meeting":
        workspace["lastOpenFiles"].insert(1, "90_Dataview/20_Meetings_Index.md")
    write_text(obsidian / "workspace.json", json.dumps(workspace, ensure_ascii=False, indent=2))


def readme(profile: str, vault: Path) -> str:
    if profile == "meeting":
        return f"""
# Hermes Meeting Vault

This vault stores real meeting minutes and meeting-related materials for governed Hermes + Obsidian workflows.

## Directory Layout

```text
00_Inbox/              Temporary drop area before filing
10_Raw/                Read-only source material
10_Raw/Meetings/       Original meeting minutes
10_Raw/Materials/      Meeting-related files and background materials
10_Raw/converted/      Markdown converted from non-Markdown sources
20_Notes/              Human or AI structured notes
20_Notes/Meetings/     Structured meeting notes generated from raw minutes
30_Cards/              Cross-meeting reusable knowledge cards
40_Concepts/           Stable reusable concepts
50_Projects/           Project or workstream notes
90_Dataview/           Dataview dashboards and indexes
_system/               Rules, prompts, metadata, templates, reports, skills
```

## Suggested Raw Filing

Use date-first names:

```text
10_Raw/Meetings/YYYY-MM-DD Topic.md
```

Related materials can go under:

```text
10_Raw/Materials/YYYY-MM-DD Topic/
```

Vault path:

```text
{vault}
```
"""
    return f"""
# Hermes Obsidian Vault

This vault stores source materials and generated Markdown artifacts for governed Hermes + Obsidian workflows.

## Directory Layout

```text
00_Inbox/      Temporary inputs waiting for review
10_Raw/        Read-only source materials
20_Notes/      Human or AI structured notes
30_Cards/      Reusable knowledge cards
40_Concepts/   Stable concept pages
50_Projects/   Project-oriented outputs
90_Dataview/   Dataview dashboards and indexes
_system/       Rules, prompts, metadata, templates, reports, skills
```

Vault path:

```text
{vault}
```
"""


def agents(profile: str) -> str:
    purpose = (
        "meeting minutes and meeting-related materials"
        if profile == "meeting"
        else "source materials and governed knowledge workflows"
    )
    return f"""
# AGENTS

## Project Goal

This directory is an Obsidian vault for {purpose}.

## Hard Boundaries

- Treat `10_Raw/` as read-only source material.
- Do not overwrite, rewrite, rename, move, or delete files under `10_Raw/`.
- Only create derived files in `20_Notes/`, `30_Cards/`, `40_Concepts/`, `50_Projects/`, `90_Dataview/`, and `_system/`.

## Write Rules

- Only write to the vault when the user explicitly asks for a concrete vault artifact.
- Preserve source traceability in every derived file.
- Prefer Dataview-compatible frontmatter.
- Do not create concept pages by default.
- Keep links conservative.

## Metadata Model

```yaml
---
type:
source:
status: draft
created:
domains:
---
```
"""


def ingest_rules(profile: str, vault: Path, skill_repo: str | None) -> str:
    skill_text = skill_repo or "<external skill repository path>"
    if profile == "meeting":
        return f"""
# Hermes Meeting Ingest Rules

```text
你正在操作一个 Hermes + Obsidian 会议纪要 Vault。

Vault 路径：
{vault}

外部 skill 仓库：
{skill_text}

基本规则：
1. 10_Raw 是只读原始材料区，不允许覆盖、改写、重命名、移动或删除。
2. 只有我明确要求写入、摄取会议、生成会议笔记、生成卡片、生成项目笔记或生成概念页时，才允许写入。
3. 会议纪要默认结构化产物写入 20_Notes/Meetings。
4. 跨会议可复用的知识才写入 30_Cards。
5. 稳定且跨材料复用的概念才写入 40_Concepts。
6. 项目、工作流、长期事项写入 50_Projects。
7. 每次写入后，在 _system/reports 写一份 ingest log。
```
"""
    return f"""
# Hermes Ingest Rules

```text
你正在操作一个 Hermes + Obsidian Vault。

Vault 路径：
{vault}

外部 skill 仓库：
{skill_text}

基本规则：
1. 10_Raw 是只读原始材料区，不允许覆盖、改写、重命名、移动或删除。
2. 只有我明确要求写入知识库或生成具体 vault 产物时，才允许写入。
3. 原始材料必须保留来源路径。
4. 知识卡片写入 30_Cards。
5. 稳定概念页写入 40_Concepts。
6. 项目材料写入 50_Projects。
7. 每次写入后，在 _system/reports 写 ingest log。
```
"""


def meeting_prompt(vault: Path, skill_repo: str | None) -> str:
    skill = skill_repo or "<external skill repository path>"
    return f"""
# Hermes Meeting Ingest Prompt

```text
你现在要在 Hermes + Obsidian 会议纪要 Vault 中执行受控摄取。

不要预设源文件的性质、价值或处理方式。请根据内容自行判断本轮应该生成会议笔记、项目笔记、知识卡片、概念页、source map，还是只生成 ingest log。

Vault 路径：
{vault}

外部 skill 仓库：
{skill}

请先阅读：
{vault}\\AGENTS.md
{vault}\\_system\\prompts\\hermes-ingest-rules.md
{vault}\\_system\\metadata\\concept-registry.md

本次源文件或源文件组：
<在这里填入 10_Raw/Meetings 或 10_Raw/Materials 下的一个或多个源文件路径>

硬性规则：
1. 不允许修改、覆盖、重命名、移动或删除 10_Raw 下任何文件。
2. 必须先判断材料性质，再决定产物类型。
3. 默认优先考虑生成 20_Notes/Meetings 下的结构化会议笔记。
4. 不要默认创建知识卡片，只有跨会议可复用的内容才创建 30_Cards。
5. 不要默认创建概念页。
6. 每轮必须创建 ingest log 到 _system/reports。

完成后汇报：只读合规、材料性质、创建文件、未创建产物及原因、项目笔记/卡片/概念页判断、概念复用、候选概念、下一步建议。
```
"""


def dataview_files(profile: str) -> dict[str, str]:
    files = {
        "90_Dataview/00_Dashboard.md": """
---
type: dashboard
status: active
---

# Dashboard

## Raw Sources

```dataview
table file.folder as Folder, file.mtime as Modified
from "10_Raw"
sort file.mtime desc
```

## Knowledge Cards

```dataview
table source as Source, status as Status, domains as Domains, file.mtime as Modified
from "30_Cards"
where type = "knowledge-card"
sort file.mtime desc
```

## Recent Changes

```dataview
table file.folder as Folder, file.mtime as Modified
from ""
where !startswith(file.path, ".obsidian")
sort file.mtime desc
limit 20
```
""",
        "90_Dataview/10_Raw_Index.md": """
---
type: dashboard
status: active
---

# Raw Source Index

```dataview
table file.folder as Folder, file.mtime as Modified
from "10_Raw"
sort file.mtime desc
```
""",
        "90_Dataview/30_Cards_Index.md": """
---
type: dashboard
status: active
---

# Knowledge Cards Index

```dataview
table source as Source, status as Status, domains as Domains, file.mtime as Modified
from "30_Cards"
where type = "knowledge-card"
sort file.mtime desc
```
""",
        "90_Dataview/40_Concepts_Index.md": """
---
type: dashboard
status: active
---

# Concepts Index

```dataview
table status as Status, domains as Domains, file.inlinks as Inlinks, file.outlinks as Outlinks
from "40_Concepts"
where type = "concept"
sort length(file.inlinks) desc
```
""",
        "90_Dataview/50_Projects_Index.md": """
---
type: dashboard
status: active
---

# Projects Index

```dataview
table status as Status, domains as Domains, source as Source, file.mtime as Modified
from "50_Projects"
where type = "project-note"
sort file.mtime desc
```
""",
        "90_Dataview/90_System_Index.md": """
---
type: dashboard
status: active
---

# System Index

```dataview
table status as Status, scope as Scope, file.mtime as Modified
from "_system"
sort file.mtime desc
```
""",
        "90_Dataview/README.md": """
# Dataview Area

This folder contains Dataview dashboards and indexes.
""",
    }
    if profile == "meeting":
        files["90_Dataview/20_Meetings_Index.md"] = """
---
type: dashboard
status: active
---

# Meeting Notes Index

```dataview
table meeting_date as Date, topic as Topic, participants as Participants, projects as Projects, status as Status, source as Source
from "20_Notes/Meetings"
where type = "meeting-note"
sort meeting_date desc
```
"""
        files["90_Dataview/00_Dashboard.md"] = """
---
type: dashboard
status: active
---

# Meeting Dashboard

## Recent Meeting Notes

```dataview
table meeting_date as Date, topic as Topic, participants as Participants, projects as Projects, status as Status
from "20_Notes/Meetings"
where type = "meeting-note"
sort meeting_date desc
limit 20
```

## Open Action Items

```dataview
task
from "20_Notes/Meetings"
where !completed
sort file.mtime desc
```

## Raw Meeting Sources

```dataview
table file.folder as Folder, file.mtime as Modified
from "10_Raw/Meetings"
sort file.name desc
```
"""
    return files


def metadata_files(profile: str) -> dict[str, str]:
    files = {
        "_system/metadata/source-map.md": """
---
type: metadata
status: active
scope: "10_Raw"
---

# Source Map

Use ingest logs for detailed per-run records.
""",
        "_system/metadata/concept-registry.md": """
---
type: metadata
status: active
scope: "40_Concepts"
---

# Concept Registry

Use this file to decide whether to reuse an existing concept, create a new concept, or defer concept creation.

## Rules

1. Check this registry before creating concept pages.
2. Prefer reuse when the new material is an example, child scenario, product implementation, or workflow extension of an existing concept.
3. Create a concept page only when the concept is stable, reusable across files, and clearly bounded.
""",
    }
    if profile == "meeting":
        files["_system/metadata/meeting-registry.md"] = """
---
type: metadata
status: active
scope: "20_Notes/Meetings"
---

# Meeting Registry

```dataview
table meeting_date as Date, topic as Topic, participants as Participants, projects as Projects, status as Status, source as Source
from "20_Notes/Meetings"
where type = "meeting-note"
sort meeting_date desc
```
"""
    return files


def template_files(profile: str) -> dict[str, str]:
    files = {
        "_system/templates/ingest-log-template.md": """
---
type: report
status: draft
created:
source:
---

# Ingest Log

## Task
## Input Sources
## Pre-reads
## Material Classification
## Created Files
## Files Not Created And Why
## Concept Reuse
## Raw Source Rule
## Next Step
""",
    }
    if profile == "meeting":
        files["_system/templates/meeting-note-template.md"] = """
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

# <Meeting Date> <Topic>

## 一句话摘要
## 会议信息
## 背景
## 讨论要点
## 决策
## 行动项
## 风险与问题
## 待确认事项
## 涉及项目或工作流
## 可沉淀知识
## 来源
"""
    return files


def copy_base_concepts(template: Path, vault: Path) -> int:
    count = 0
    concept_src = template / "40_Concepts"
    concept_dst = vault / "40_Concepts"
    if concept_src.exists():
        for src in concept_src.glob("*.md"):
            shutil.copy2(src, concept_dst / src.name)
            count += 1
    registry_src = template / "_system/metadata/concept-registry.md"
    if registry_src.exists():
        shutil.copy2(registry_src, vault / "_system/metadata/concept-registry.md")
    return count


def setup(args: argparse.Namespace) -> None:
    vault = Path(args.vault_path).expanduser().resolve()
    template = Path(args.template_vault).expanduser().resolve() if args.template_vault else None

    if is_non_empty(vault) and not args.force_empty:
        raise SystemExit(f"Target vault is non-empty. Pass --force-empty to allow writing: {vault}")

    created_dirs = []
    for rel in BASE_DIRS + PROFILE_DIRS[args.profile]:
        path = vault / rel
        path.mkdir(parents=True, exist_ok=True)
        created_dirs.append(rel)

    if template and args.copy_obsidian_config:
        copy_tree(template / ".obsidian", vault / ".obsidian")
    normalize_workspace(vault, args.profile)

    copied_concepts = 0
    for rel, content in metadata_files(args.profile).items():
        write_text(vault / rel, content)
    if template and args.copy_base_concepts:
        copied_concepts = copy_base_concepts(template, vault)

    if template and args.copy_skill_note:
        src = template / "_system/skills/hermes-obsidian-controlled-ingest.skill.md"
        if src.exists():
            shutil.copy2(src, vault / "_system/skills/hermes-obsidian-controlled-ingest.skill.md")

    write_text(vault / "README.md", readme(args.profile, vault))
    write_text(vault / "AGENTS.md", agents(args.profile))
    write_text(vault / "_system/prompts/hermes-ingest-rules.md", ingest_rules(args.profile, vault, args.skill_repo))
    if args.profile == "meeting":
        write_text(vault / "_system/prompts/hermes-meeting-ingest-prompt.md", meeting_prompt(vault, args.skill_repo))

    for rel, content in dataview_files(args.profile).items():
        write_text(vault / rel, content)
    for rel, content in template_files(args.profile).items():
        write_text(vault / rel, content)

    report = f"""
---
type: report
status: complete
created: {date.today().isoformat()}
source: {template or "none"}
---

# Vault Setup

## Vault

`{vault}`

## Profile

`{args.profile}`

## Created Directories

{chr(10).join(f"- `{item}`" for item in created_dirs)}

## Template Components

- copied obsidian config: {bool(template and args.copy_obsidian_config)}
- copied base concepts: {copied_concepts}
- copied skill note: {bool(template and args.copy_skill_note)}

## Safety

Raw source folders were initialized but no raw sources were copied.
"""
    write_text(vault / f"_system/reports/vault-setup-{date.today().isoformat()}.md", report)

    json.loads((vault / ".obsidian/workspace.json").read_text(encoding="utf-8"))
    print(f"Vault initialized: {vault}")
    print(f"Profile: {args.profile}")
    print(f"Created directories: {len(created_dirs)}")
    print(f"Copied base concepts: {copied_concepts}")
    print("workspace.json ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a governed Hermes + Obsidian vault.")
    parser.add_argument("--vault-path", required=True, help="Target vault path")
    parser.add_argument("--profile", choices=["general", "meeting"], default="general")
    parser.add_argument("--template-vault", help="Optional template vault path")
    parser.add_argument("--skill-repo", help="Optional external skill repository path to write into prompts")
    parser.add_argument("--copy-obsidian-config", action="store_true")
    parser.add_argument("--copy-base-concepts", action="store_true")
    parser.add_argument("--copy-skill-note", action="store_true")
    parser.add_argument("--force-empty", action="store_true", help="Allow writing into an existing non-empty target")
    setup(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
