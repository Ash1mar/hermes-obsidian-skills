# ADR-0004：稳定知识身份与显式 Finalize

日期：2026-09-14，2026-09-15 校正。状态：接受；P3–P7 待实施。关联：[来源单元契约](0003-source-unit-contracts.md)和[演进计划](../SOURCE_UNITS_EVOLUTION_PLAN.md)。

## 背景

P0/P1 已建立来源单元契约和新库骨架，P2 已将解析后的规范内容发布为可回读 SourceUnit。此前知识构建的候选、证据同步、页面复核、ledger 终态、链接整理和 Provider 同步分散在 controlled-ingest 中。候选 ID 只在单次构建中有效，页面路径/slug 也不能承担跨运行稳定对象身份。

WeKnora 的可借鉴流程可抽象为候选发现、分批 chunk citation、按对象 Reduce 和全库 Finalize。我们保留更严格的来源、QA、业务版本和只读查询边界，不复制其数据库、分布式调度或引用降级策略。

## 决策

1. SourceUnit 在 P2 通过确定性 `unit_ref` 成立，并作为知识构建与 RAG 共用的 canonical chunk；文件式 `manifest + JSONL` 是完整首个后端，数据库不是身份语义的起点。
2. P3 增加稳定 `subject_id/page_id`。候选 ID 仍是单次运行记录；slug、文件路径、别名和目录是可变投影，不能改变对象身份。
3. P3 将知识构建明确为 Pass 0 候选、Pass 1..N 单元引用、Reduce 页面修订和 Build Finalize。Build Finalize 提交复核、provenance、依赖与任务终态。
4. P4 新增增量 Vault Finalize：计算受影响集合，核对页面修订和来源依赖，处理 stale/withdrawn 贡献，整理别名、重定向、链接和目录投影，并发布可审计的 knowledge release manifest。
5. 新增 `hermes-obsidian-knowledge-finalize` Skill 作为 P4 操作入口。先实现可测试的领域模块与 `plan/apply/validate`，Skill 不成为后台服务，也不自动批准业务版本。
6. Finalize 输出索引资格集合，但不隐式修改 Provider。P5 Provider sync 直接消费可索引 SourceUnit 和已提交页面修订，不普遍重切来源；原始来源检索不被全库导航收尾阻塞。
7. WorkLedger 不再拥有内容边界。它保留任务领取、实际检查、覆盖、QA、重试、执行者、输出和完成依据，是工作/审计控制面，不因数据库出现而消失。
8. 数据库作为 repository backend 后置。它改善查询、唯一约束、事务、并发和影响分析；`pg_trgm` 只用于名称/别名候选预筛选，不能决定对象同一性。禁止长期 JSON/SQL 双写。

## 两级 Finalize

| 层级 | 输入 | 结果 |
|---|---|---|
| Build Finalize | 一次 Reduce 的页面修订、unit refs、review 和任务 revision | 提交页面依赖与构建终态，失败可按运行恢复 |
| Vault Finalize | 多个已提交 build、受影响 page/subject、当前来源资格 | 链接/目录/别名投影、stale/blocked 清单、knowledge release manifest |

Vault Finalize 是增量一致性操作，不是全库巨型事务或发布锁。页面修订可以先完成，导航随后合并收尾；部分失败必须留有明确记录，不能用成功页面掩盖失败对象。

## 阶段

P2 来源内容层；P3 Pass/Reduce、知识身份及 Build Finalize；P4 Vault Finalize 与新 Skill；P5 Provider/query；P6 文件式完整实践和故障演练；P7 发布及正式新库重建。数据库 adapter 在文件式端到端语义稳定后单列实施，可在正式重建前加入，但不得重新定义 ID 和状态合同。

## 后果

controlled-ingest 继续负责来源准备、Unit 发布、阅读任务、候选和页面修订生成；不再独自承担全库收尾。lint 保持独立只读审计。Provider 仍是可重建数据面。目录移动、slug 调整和数据库迁移都不改变 SourceUnit 或知识对象身份。
