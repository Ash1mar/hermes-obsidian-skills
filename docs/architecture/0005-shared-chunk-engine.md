# ADR-0005：P2.1 共享 Chunk Engine 校正

日期：2026-09-15。状态：接受；P2.1 已实现并通过验收。关联：[来源单元契约](0003-source-unit-contracts.md)、[知识身份与 Finalize](0004-knowledge-identity-and-finalize.md)、[P2.1 验收](../SOURCE_UNITS_P2_1_ACCEPTANCE.md)和[演进计划](../SOURCE_UNITS_EVOLUTION_PLAN.md)。

## 背景

P2 已经把规范正文、SourceSection、owned ranges、SourceUnit、精确坐标和文件式发布实现出来，并明确 SourceUnit 是知识构建与 RAG 共用的 canonical chunk。但是切片分析、策略路由、结构保护、overlap、诊断和 SourceUnit 发布仍集中在 `FileSourceUnitService`；当前策略路由也没有形成可重放的候选验证与回退报告。若直接进入 P3，之后再调整切片引擎会改变 `unit_set_id/unit_id`，使已经产生的知识引用和工作记录全部重建。

## 决策

1. 在 P2 与 P3 之间增加强制门禁 **P2.1：共享 Chunk Engine 校正**。P3 只能消费通过 P2.1 发布的 UnitSet v2。
2. Chunk Engine 属于 `hermes-source-units` 来源内容层，以标准库 Python 实现并随需要它的 Skill 内嵌交付；它不是 Provider 子模块、独立服务或新的运行环境。
3. 引擎采用两阶段处理：先从可靠 Bundle outline、Markdown 标题、启发式结构或 root fallback 建立 SourceSection/owned ranges；再在每个 owned range 内 atomize、切分、合并、施加有限 overlap 和验证。
4. 支持 `auto`、`structure`、`heuristic`、`recursive`。候选结果必须检查覆盖、越界、异常碎片、maximum、结构保护和 overlap；失败按策略链回退并记录拒绝原因，末级使用 `preserve-and-report`。
5. 保留代码围栏、块公式、Markdown 表格、连续列表和来源资产等结构。受保护结构超过 maximum 时完整保留、标记 `oversized-protected-structure`，Provider 不得静默截断。
6. `CanonicalChunkSpan` 只表达 section、绝对 codepoint span、结构与质量信息；`FileSourceUnitService` 再结合 artifact、UnitSet 和内容哈希生成确定性 Unit ID。未来文件或 SQL repository 共用同一引擎。
7. SourceSection 提供父级结构，SourceUnit 是可检索子块，`SourceUnitReader.context` 组合祖先及相邻上下文。暂不复制一套父块正文；未来 parent-context projection 也不能成为第二套 canonical chunk。
8. 影响 canonical 身份的参数集中到 `source.chunking`。token budget 支持 `off/audit/hard`，Chunk Engine 只依赖轻量 `TokenCounter` 接口。真实 embedding tokenizer 由 P5 adapter 提供；未提供 counter 时不得声称完成 token audit。
9. 配置升级为 `hermes-source-unit-config/v2`，UnitSet 升级为 `hermes-source-unit-set/v2`；SourceSection、SourceUnit 和 UnitRef 保持 v1。P2 试验 UnitSet 不迁移。
10. UnitSet v2 钉住 `engine_version`、`engine_fingerprint`、`effective_config_fingerprint`、`engine_report_path` 和 `engine_report_sha256`。每次发布保存不可变 `engine.json`，包含有效配置、文档画像、尝试与拒绝、最终策略、大小统计、token audit、oversized 和 coverage 结果。

## 与后续阶段的关系

- P3 使用 P2.1 UnitRef、reading window 和 engine/unit-set 代次，不再建立自己的正文边界。
- P4 在 Build/Vault Finalize 中检查来源依赖是否仍指向允许的 engine/unit-set 代次。
- P5 对普通来源执行一 SourceUnit 对应一索引文档；标题渲染后用真实 tokenizer 预检，超限时阻断或只对已报告特例建立精确 subspan。
- P6 增加策略质量、token 分布、回退解释及 Wiki/Provider 共用 UnitRef 的端到端验收。
- P7 只从原件生成 P2.1 UnitSet，不导入 P2 试验产物。

## 验收门禁

相同输入、配置和引擎版本必须产生相同 UnitSet；owned range 覆盖率必须为 100%；Unit 与 overlap 不得跨 owned range；受保护结构不得被普通切片破坏；所有回退和 oversized 均可解释；中英文、emoji、CRLF、分页及编号章节 fixture 必须通过；完整 Skill 复制后仍可在 `python -I -S` 下工作。P5 接入完成后还必须证明普通索引文档与 SourceUnit 一一对应，且 qmd-like-rag 新链路不再调用旧 `chunk_markdown_file()`。

## 后果

P2.1 会重建 P2 fixture 的 UnitSet 身份，但当前尚无 P3 知识引用或正式新库，因此这是变更成本最低的时点。P3–P7 的目标和顺序不变，其输入契约更严格。数据库仍是同一 repository 接口的后置实现，不能复制或重新定义 Chunk Engine。
