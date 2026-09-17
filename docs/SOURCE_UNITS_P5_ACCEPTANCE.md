# P5 Release 驱动检索投影验收

日期：2026-09-17。范围：P5.1–P5.5 代码链路与 P5.6 自动化门禁。状态：仓库实现和 main WSL Provider 0.5 运行时部署通过；新 release generation、真实材料检索及内网模型服务联调待执行。

## 验收结论

P5 已把 qmd-like-rag 的来源入口从“扫描 Markdown 并自行切 chunk”改为“读取 P4 当前 knowledge release 并投影其中获准的 canonical SourceUnit 与 knowledge page”。SourceUnit 仍由共享来源层定义；Provider 只持有可重建的检索投影，Query 使用完整 UnitRef 或精确 subspan 从来源层回读证据。

本阶段没有兼容或迁移旧 Provider chunk，也没有建立实际知识库。首次部署必须从当前 release 创建全新的 index generation。

## 已交付

| 门禁 | 实现与证据 |
|---|---|
| P5.1 release 语料 | `corpus.py` 校验 current release ID/hash，只枚举 `index_eligibility` 中获准的 `source_unit_set` 与 `knowledge_page`；旧 Markdown glob 和 `chunker.py` 已删除。 |
| P5.2 renderer/tokenizer | 普通 SourceUnit 一 Unit 一投影；标题仅进入检索文本。Provider 从明确的 `tokenizer.json` 加载实际 tokenizer，校验可选 SHA-256，不再使用 `cl100k_base` 或字符回退。普通超限阻断；只有 engine report 标记的超大受保护结构可生成无重叠精确 subspan。 |
| P5.3 generation | 新 generation 位于 Provider 主机状态目录；state 与 projection manifest 钉住 release、renderer、tokenizer、模型、projection 与 Chroma/BM25 generation。状态指针最后发布，中断代次不会冒充 ready。 |
| P5.4 协议 | `hermes-coarse-recall/v1` 保持 `candidate-navigation-only`；CLI/HTTP/Chroma/BM25/normalizer 增加 SourceUnit capability、projection identity、release 和 generation 字段。缺少新能力的 Provider 被 Query 拒绝。 |
| P5.5 Query | Query 先核对当前 release、generation、eligibility 和完整 UnitRef，再通过 `FileSourceUnitService.get` 精确回读核心正文或 subspan。Provider snippet 不作为证据正文。 |
| Finalize 所有权 | release sync 入口及配置从 Ingest Skill 移到 Knowledge Finalize Skill。Ingest 只发布来源内容与工作状态；Finalize apply 后可显式提交 release ID/hash 给 Provider。 |
| 新库能力 | bootstrap capability 提升为 P5/retrieval；Vault validator 只有在 retrieval manifest 2.0 与当前 release、generation、SourceUnit capability 和 tokenizer readiness 一致时才报告 query ready。 |
| 复制式运行 | canonical `hermes-source-units` 继续由生成器嵌入各 Skill，并新增 Query 与 qmd-like-rag 副本；Hermes 环境无需额外安装共享包，Provider 仍使用自身独立虚拟环境。 |

## 自动化验证

- 仓库全量：`python -m pytest -q`，232 项通过。
- P5 纵向样例从临时 P4 release 枚举真实 SourceUnit，验证每个 eligible Unit 只产生一个普通来源投影。
- Query 样例让 Provider 返回不可信 snippet，同时携带 UnitRef/subspan；结果正文由 SourceUnit reader 精确回读，等于请求的原文范围。
- renderer 正反例验证普通超限会失败，显式 oversized protected Unit 才能生成连续、无重叠、完整覆盖且各自不超预算的 subspan。
- tokenizer 测试验证发布者资产可加载且校验和不匹配会失败。
- Provider/Skill 测试验证显式 release sync、manifest 2.0、默认只读 Query、缺失 Provider fallback 及分支配置边界。

## 尚未宣称完成的运行验收

main WSL 已从 Provider 0.4.0 升级到 0.5.0，复用了固定 revision 的本地模型，写入并验证 embedding/reranker tokenizer SHA-256；`doctor` 确认 CUDA 13.0 与 NVIDIA GeForce RTX 5070 Ti Laptop GPU 可用，五个 Skill 已同步。由于尚无 P5 新库和当前 release，`status` 正确为 `absent`，两个 Provider adapter 保持关闭，也没有创建 Chroma/BM25 generation。以下项目仍是 P5.6/P6 的运行门禁：

1. main 本地模型拓扑针对真实 P5 release 执行 sync/recall，并核对 generation 与检索质量。
2. intranet 远端模型拓扑的 CLI/HTTP 等价性、只读 Vault 挂载、host bind state、超时和故障恢复。
3. stale release、索引中断及 tokenizer/model 资产变更在实际进程中的拒绝与重建。
4. 新 generation 完整重建后的检索质量与无答案行为；这些结果进入 P6 实践报告。

只有上述门禁完成后，才能把 P5 标记为部署完成并开始正式 P6 端到端材料实践。P7 仍负责一致发布、正式 bootstrap 和全量 ingest。
