# ADR-0006：P5 Release 驱动的 SourceUnit 检索投影

日期：2026-09-16，2026-09-17 实施更新。状态：接受；P5.1–P5.5 与自动化门禁已实现，P5.6 的真实部署拓扑验收待执行。关联：[SourceUnit 契约](0003-source-unit-contracts.md)、[显式 Finalize](0004-knowledge-identity-and-finalize.md)、[共享 Chunk Engine](0005-shared-chunk-engine.md)、[P5 验收](../SOURCE_UNITS_P5_ACCEPTANCE.md)和[演进计划](../SOURCE_UNITS_EVOLUTION_PLAN.md)。

## 背景

P2/P2.1 已把解析后的规范正文发布为 canonical SourceUnit，P3 用同一 UnitRef 完成知识构建，P4 再发布带索引资格的 knowledge release。现有 qmd-like-rag 仍扫描 Markdown glob，以自己的标题父段、token 子窗口和 overlap 建立 Provider chunk；其候选依赖路径、粗行号与 `parent_text`。继续沿用这条入口会产生第二套内容边界，使 P4 release、SourceUnit 身份和精确回读不能约束检索数据面。

qmd-like-rag 仍是独立 Provider。它的 CLI/HTTP 传输、Chroma/BM25 混合召回、本地或远端模型后端及可重建主机状态都有价值；P5 只替换语料权威、投影身份和回读链路，不把重依赖复制进 Hermes Skill。

## 决策

1. P5 新链路只消费 P4 当前 knowledge release。Provider 从 release 的 eligibility 枚举 `source_unit_set` 与 `knowledge_page` 投影，不再通过 `include_patterns` 扫描来源正文，也不再调用 `chunk_markdown_file()` 生成普通来源 chunk。同步由 release submission 显式触发，Query 保持只读。
2. 一个 eligible canonical SourceUnit 对应一个普通索引文档。标题路径可以作为 renderer 生成的检索上下文加入 embedding/BM25 输入，但不进入核心正文、Unit hash 或 SourceUnit 定位。Provider 不普遍合并、重切或增加 overlap。
3. `source_unit` 与 `knowledge_page` 是两种不同投影。前者保存完整 UnitRef；后者保存 `page_revision_id` 及其 active source refs。派生知识页不得伪装成原始来源单元。
4. 正常 Unit 的渲染结果超过 embedding 模型硬上限时，索引构建必须失败并报告对象。只有 Chunk Engine 已显式报告的 oversized 受保护结构才可产生精确 subspan projection；subspan 必须保留父 UnitRef、半开区间、内容 hash 与特例原因，不能形成第二套 canonical Unit。
5. token 上限使用 embedding 模型实际 tokenizer 测量。Provider 必须钉住 tokenizer 运行库及模型发布者提供的资产（如 `tokenizer.json`、tokenizer 配置或 SentencePiece 文件），记录身份、不可变 revision/校验和和 fingerprint。缺失、不匹配或不可加载时拒绝 sync，不得静默回退到 `cl100k_base`、字符估算或其他 tokenizer。reranker 若使用不同 tokenizer，单独记录并检查。旧 chunker 删除且无其他调用后移除 `tiktoken`。
6. 每个 index generation 钉住 release ID/hash、UnitSet/Unit 或 page revision、renderer 版本/fingerprint、embedding tokenizer 与模型 fingerprint、reranker fingerprint，以及 Chroma/BM25 generation。内容、renderer、tokenizer/模型和存储损坏分别形成可解释的失效原因。
7. 保留 `hermes-coarse-recall/v1` 的 `candidate-navigation-only` 权威边界，并增加 `source_units` capability、完整 UnitRef、projection kind/fingerprint、release ID/hash 和 index generation。CLI、HTTP、Provider normalizer 与 Query adapter 必须端到端保留这些字段；旧 Provider 缺少能力时，新 P5 Vault 明确拒绝使用。
8. Query 在使用候选前校验其 generation 与当前 release，再校验 eligibility 和完整 UnitRef。核心正文由 SourceUnit reader 精确回读；相邻 Unit、章节、资产和派生页来源按 token 预算另行组合并去重。Provider 的 snippet、标题上下文和旧 `parent_text` 都不是最终证据。回答仍需回到当前原文或 PDF 验证。
9. P5 首次部署创建全新的 index generation，从当前 release 完整重建；不迁移旧 Markdown chunk 或复用其索引状态。SourceUnit 身份不因 embedding、renderer 或索引代次改变。
10. Provider 状态继续位于运行主机、Vault 之外。正式 intranet 默认使用可直接检查和备份的 host bind path，例如 `${HERMES_HOST_DATA_ROOT}/phq/qmd-like-rag-state`；Vault 只读挂载。named volume 可用于开发或演练，不是架构要求。Hermes 与 Provider 保持独立容器并通过受控网络通信。
11. intranet 镜像应在可联网构建环境固定依赖、模型/tokenizer 资产和校验和后导出，并记录镜像摘要再导入。只有具备内部包源、基础镜像、模型资产和扫描流程时才在内网构建。密钥通过运行环境或 secret 注入，不进入 Vault、镜像或可移植配置。

## 分阶段实施

| 阶段 | 交付 |
|---|---|
| P5.1 | Release reader、eligibility corpus 和同步入口；新链路删除 Markdown glob 与 Provider 独立 chunker |
| P5.2 | 一 Unit 一文档 renderer；两类 projection；真实 tokenizer 绑定；超限阻断与 oversized subspan |
| P5.3 | generation manifest、分层 fingerprint、Chroma/BM25 原子发布及全新重建 |
| P5.4 | CLI/HTTP capability 与完整 UnitRef；Provider/Query adapter 保真和拒绝旧能力 |
| P5.5 | Query 校验 release/generation、精确回读、预算化上下文、层级候选融合和最终证据核验 |
| P5.6 | 端到端、失效、故障、无答案、派生页溯源、只读及部署验收 |

## 验收门禁

- 普通 SourceUnit 与 `source_unit` 索引文档一一对应；新链路没有 `chunk_markdown_file()`、普通重切或隐式 overlap。
- tokenizer 身份、资产校验和、最大输入长度和 readiness 可由 doctor/status/capability 检查；错误 tokenizer 阻止 sync。
- release、UnitSet、renderer、模型/tokenizer 任一变化都产生正确的新 generation 或明确失效，不混读旧代次。
- 两种 projection 的身份和溯源不可互换；所有来源候选都可用完整 UnitRef 精确回读并验证 hash。
- stale release、撤回来源、Provider 不可用、索引中断与无答案均有明确结果；Query 不重建、不修复、不绕过 eligibility。
- CLI 与 HTTP 返回同一契约；`main` 本地模型和 `intranet` 远端模型拓扑都通过能力及故障测试。

## 后果

P5 会主动打破旧 qmd-like-rag 索引格式和配置语义。`include_patterns`、`chunk_size`、`chunk_overlap`、`parent_text` 及旧文件 fingerprint 不再属于 SourceUnit 新链路；旧索引只能作为历史运行状态被丢弃。Provider 仍可替换，Vault release 和 SourceUnit repository 继续是控制面权威，Chroma/BM25 generation 仍可完整重建。

## 实施记录

2026-09-17 的仓库实现将 qmd-like-rag 升级到 0.5：删除 Provider 独立 Markdown chunker 与 `tiktoken`，从当前 knowledge release 枚举 eligible SourceUnit/knowledge page，以发布者 tokenizer 资产执行一 Unit 一普通投影。普通超限会阻断同步；只有 Chunk Engine 报告的超大受保护结构可生成无重叠精确 subspan，候选始终保留父 UnitRef。

索引以新 generation 写入 Vault 外状态目录，manifest 钉住 release、renderer、tokenizer、模型和 projection；Recall 拒绝配置、模型、tokenizer 或 release 不匹配的代次。Finalize Skill 持有显式 release sync 入口，Query 校验能力与 eligibility 后通过 SourceUnit reader 回读核心或 subspan。仓库自动化验收已通过；尚未在本次实施中安装 0.5 WSL 运行时、下载或核验生产 tokenizer/model 资产，也未创建实际索引，因此部署态 P5.6 仍是后续门禁。
