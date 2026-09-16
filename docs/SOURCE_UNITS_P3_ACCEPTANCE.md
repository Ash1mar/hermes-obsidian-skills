# P3：Pass/Reduce、稳定知识身份与 Build Finalize 验收

日期：2026-09-16。范围：本地 `main` 文件式实现；不迁移旧内容，不创建正式 Vault，不执行 P4 Vault Finalize 或 P5 Provider 投影。

## 交付

- `FileKnowledgeBuildService` 直接消费 UnitSet v2。任务以完整 UnitRef 为 target，使用 revision、actor、attempt、重叠任务报告和 blocked/failed/skipped 恢复状态；ledger 不再拥有正文边界。
- 阅读操作发布 `hermes-reading-package/v1`，保存 core/context 角色、实际文本或资产信息、Unit/选区 hash、标题、质量引用、预算和 omitted refs。context 可以在显式检查后支持候选，但不计入 task target 覆盖。
- Pass 0 记录候选，Pass 1..N 记录 chunk citation；sequence 连续且幂等，候选支持必须在本次 reading package 的实际 inspections 内。候选同时记录 applicability、conditions 和 exceptions。
- Reduce 只消费 citation Pass，要求 target 完整检查且没有 deferred 范围。多来源支持合并为稳定 subject/page 的页面修订；候选 ID 仍属于单次 Pass，路径和别名只是投影。
- `subject_id` 由 Vault、kind 和显式 identity key 决定，`page_id` 由 subject 决定。相似名称不自动合并；create 与 update/reuse/relate 分别要求不存在或已存在的稳定 identity。移动页面路径不改变 subject/page ID。
- 页面 revision sidecar 钉住 parent、正文 hash、支持 UnitRefs、QA、业务资格、可见性和 review。P3 提交后保持 `business_status: unassessed`、`visibility: draft`；QA 单独为 `usable` 或 `qa_required`。
- Build Finalize 在写入前验证 task/build revision、UnitRefs、identity/path 冲突、draft/parent hash 和每页 review。页面、revision、identity registry 和 task 写完后才提交 run manifest；中断后可按同一 run 幂等恢复。
- 零候选任务可以在完整检查和明确理由下完成，不伪造页面或知识 identity。Vault lint 会验证所有 P3 run、revision sidecar、稳定 identity 和当前页面 hash。
- 新入口为 controlled-ingest Skill 内的 `scripts/manage_knowledge_build.py`。共享运行时版本 `0.3.0`，仍无第三方运行时依赖并生成到 bootstrap、lint 和 controlled-ingest 的 `lib/`。

## 验证

- 专项覆盖双来源 Pass/Reduce/Finalize、QA 状态传播、路径移动保持身份、历史 run 回查、零候选完成、重叠任务、中断重试、Pass 幂等和拒绝用 Pass 0 代替 citation。
- bootstrap 生成 P3 capability 和空 identity registry；隔离 Skill 继续在 `python -I -S` 下运行。
- `python -m pytest -q`：`219 passed`。

## 阶段边界

P3 的 Build Finalize 只提交一次知识构建的页面 revision、provenance、identity 和任务终态。它不计算全 Vault 受影响集合，不处理来源撤回后的剩余支持，不整理重定向/目录，也不发布 release manifest；这些属于 P4。P3 不修改 Provider 或旧 qmd-like-rag 切片；这些属于 P5。
