# 当前 Wiki 知识构建指南

日期：2026-09-23。本文说明当前 P5 SourceUnit Vault 的知识构建路径。命令参数和强制门禁以 [Governed Ingest Orchestrator](../hermes-obsidian-governed-ingest-orchestrator/SKILL.md)、[Controlled Ingest](../hermes-obsidian-controlled-ingest/SKILL.md)、[Knowledge Finalize](../hermes-obsidian-knowledge-finalize/SKILL.md)及直接 reference 为准；本文不授权跳过人工审批。早期 v3、WeKnora 对照和当时的操作记录保存在本地 `legacy docs/`。

## 从原件到知识页

1. 明确本次请求是来源准备、知识构建，还是两者都要。已有合格的 Bundle 和 SourceUnit 可复用；原件、规范产物和当前 UnitSet 的身份必须校验。
2. 对 P5 Vault，Controlled Ingest 发布 canonical SourceUnits。UnitRef 钉住 Vault、resource、artifact revision、UnitSet 和 unit；标题与上下文是阅读辅助，不替代核心证据。
3. 先测量实际序列化的 `window + materials`，再按精确阅读预算计划和准备批次。输入、指纹或预算漂移时停止并重测，不能缩减记录以假装符合预算。
4. Pass 以持久化 slice 为执行单元。领取、心跳、完成、失败、过期回收和取消都遵守 Vault 账本与租约；幂等键相同而内容不同应报冲突。模型必须实际阅读分配的材料，记录检查范围、候选、证据和 QA。
5. Reduce 先由每个 resource 形成局部 proposal，再由单一 global coordinator 决定稳定 subject/page 身份、路径和 draft run 所有权。每个 task 只能进入一个 draft run；不得靠相似标题自动合并不同对象。
6. 人工检查点 1 批准后才可 Build Finalize。页面修订、支持 UnitRefs、review、QA、业务资格与可见性分别记录；写出 Markdown 本身不表示已发布或具备查询资格。
7. Knowledge Finalize 显式 `plan → apply → validate`，在人工检查点 2 后发布不可变 release、导航和索引资格。若部署允许，再由 `sync_release_index.py` 提交该 release 给 Provider。同步失败不取消 release，当前 release 的 `query_ready` 仍为 false。

## 持久化编排与人工责任

一次完整摄取可使用 Orchestrator 的 `dispatch_ingest_workflow.py start` 建立 Vault 权威工作流。启动时会保存十二类 worker 模板快照和 SHA-256，并尝试把可执行节点投影到 Hermes Kanban。`status`、`resume`、`cancel`、`approve` 和看板重建都围绕同一 workflow ID；重复请求须保持相同 ID 和摘要。Kanban 是可重建视图，不能仅凭卡片 `done` 判定领域操作成功。

主机当前 `worker_dispatch_enabled: false`。Gateway 不可用会保留工作流并返回 `dispatcher_unavailable`；不能报告为后台正在执行。将来启用自主派发后，Vault 仍须显式武装不可变、最多八个 Pass slice ID 的 canary allowlist。Reduce、检查点、Finalize 和发布节点不在该 canary 内。两个人工检查点都必须由人提供批准记录；worker 不能自批。`compact-3` 与 `diagnostic-6` 只改变状态呈现，不改变领域阶段和审批条件。

## 完成与失败的报告

分别报告来源保存、登记、转换、SourceUnit 发布、实际检查范围、Pass/Reduce/Build、QA、release 和检索准备状态。`processing_status: completed` 只证明转换处理，不证明知识构建完成；`qa_required` 仍待复核；一个已完成 slice 或 Kanban 卡片不代表整批成功。

失败后先读 Vault 工作流与对应领域账本，再按当前 revision 恢复。预算漂移需要重新测量；租约过期需要回收；输入、模板或审批摘要漂移必须阻断。Provider 不可用时保留已提交 release，并明确查询尚未 ready。不要通过手改 workflow JSON、补写虚假 Pass 或绕过 QA 来恢复。

## 参考入口

- [SourceUnit 与批次构建](../hermes-obsidian-controlled-ingest/references/source-units.md)
- [摄取工作流账本](../hermes-obsidian-controlled-ingest/references/workflow-ledger.md)
- [Kanban adapter](../hermes-obsidian-controlled-ingest/references/kanban-adapter.md)
- [编排工作流合同](../hermes-obsidian-governed-ingest-orchestrator/references/workflow-contract.md)
- [编排操作](../hermes-obsidian-governed-ingest-orchestrator/references/operations.md)
- [版本兼容展示](../hermes-obsidian-governed-ingest-orchestrator/references/compatibility-profiles.md)
