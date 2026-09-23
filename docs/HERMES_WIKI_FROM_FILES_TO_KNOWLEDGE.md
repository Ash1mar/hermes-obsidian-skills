# 从文件到知识：Hermes + Obsidian 当前链路

日期：2026-09-23。本文面向需要理解系统如何把文件变成可查询知识的读者。SourceUnit 的详细设计与生成过程见[SourceUnit 设计与作用](SOURCE_UNIT_DESIGN_AND_ROLE.md)。早期旧链路分析及 WeKnora 时间点对照保存在本地 Git 忽略目录 `legacy docs/`；当前合同以[官方技术规范](OFFICIAL_TECHNICAL_SPECIFICATION.md)和各 Skill 为准。

## 一份文件如何进入系统

原件保存在 Vault 的 `10_Raw/`，其内容和来源身份受到保护。MinerU、OCR 或 MarkItDown 产生可检查的规范化 Bundle；转换结果仍须经过结构、质量和治理校验。共享 Chunk Engine 在规范文本和资产上建立 canonical SourceUnits，保存稳定 UnitRef、精确坐标、内容指纹与诊断。SourceUnit 是知识构建和检索共同引用的来源核心；读取上下文及索引渲染不能改写它的身份。

Controlled Ingest 只负责来源和知识构建侧：登记、转换、发布 UnitSet、精确阅读预算、Pass、分层 Reduce 及 Build Finalize。Pass slice 有租约、心跳、重试和幂等保护；resource proposal 由 global Reduce 协调为稳定的知识对象和页面修订。模型负责判断候选、证据归属和页面内容；程序检查引用、覆盖、修订、身份与状态。自动校验不能证明每句业务结论正确，QA 与人工复核仍须独立保留。

## 工作流怎样恢复

[Governed Ingest Orchestrator](../hermes-obsidian-governed-ingest-orchestrator/SKILL.md) 用一次请求创建 Vault 权威的持久工作流，钉住十二类版本化 worker 模板，并将节点投影到可重建的 Hermes Kanban。源准备、精确计划、Pass slices、resource/global Reduce、Build Finalize、Vault Finalize plan、release apply、可选 Provider sync 和验收拥有不同的状态。两个检查点必须取得人类批准。Kanban 任务结束之后仍要核对 Vault 领域记录。

当前主机关闭自主 worker 派发。未来开放时还需 Vault 中不可变的最多八个 Pass slice allowlist；其他节点保持阻断。`compact-3` 与 `diagnostic-6` 是旧提示流程的展示兼容方式，实际仍为同一个 workflow ID 和领域图。Gateway 不可用时，工作流已保存但后台未运行。

## 何时变成可查询知识

[Knowledge Finalize](../hermes-obsidian-knowledge-finalize/SKILL.md) 将完成的 builds 和来源变更收尾为不可变 knowledge release，计算失效或撤回贡献、页面导航及索引资格。只有 release 提交后，显式 `sync_release_index.py` 才能把许可的 SourceUnits 与知识页投影到 qmd-like-rag。Provider 的 Chroma/BM25 generation、模型与缓存位于 Provider 主机；Vault 保存可审计的 release、配置、指纹和索引状态。Ingest 不维护该索引，Query 只读召回并按 UnitRef 回读来源证据。

`main` 的 Provider 0.5 运行时、固定模型和 tokenizer 已部署；截至本文日期还没有 P5 release generation，真实材料 sync/recall 与 intranet 拓扑联调未验收。代码具备工作流和检索能力，不代表新正式 Vault 已重建或自动 worker 已启用。

## 与应用服务式 Wiki 的边界

本系统采用 Vault 文件和显式记录维持来源、构建与发布状态；模型执行语义判断，脚本守住可机械检查的边界。页面身份、业务版本、来源贡献、release、检索 generation 是不同对象。与应用服务式 Wiki 相似的是候选识别、跨来源归并、页面修订和导航；当前实现的自主派发仍受 canary 限制，不能据此宣称已经具备连续自动流水线。需要操作细节时阅读[当前知识构建指南](WIKI_KNOWLEDGE_CONSTRUCTION_GUIDE.md)。
