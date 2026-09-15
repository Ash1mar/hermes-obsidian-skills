# P2.1：共享 Chunk Engine 验收

日期：2026-09-15。范围：本地 `main` 实现；不迁移旧内容，不创建正式 Vault，不接入 P3 知识构建或 P5 Provider。

## 交付

- 新增 `hermes_source_units.chunk_engine`，按 models、profile、sections、atoms、strategies、overlap、validation 和 engine 分离切片领域逻辑。`FileSourceUnitService` 负责 artifact、确定性 Unit 身份、repository 和读取，不再实现第二份切片算法。
- 支持 `auto/structure/heuristic/recursive` 候选链。每次尝试记录 accepted/reasons；可靠 outline 优先，编号、分页、分隔线和短标题可形成 heuristic outline，质量不合格时回退，recursive 只允许对非结构质量问题执行 preserve-and-report。
- 保留 fenced code、块公式、Markdown table、连续列表和图片引用。超过 maximum 的受保护结构完整保存并产生 `oversized-protected-structure`，不静默截断。
- 配置升级为 `hermes-source-unit-config/v2`，canonical 参数集中在 `source.chunking`。UnitSet 升级为 `hermes-source-unit-set/v2`，钉住 engine version/fingerprint、effective config fingerprint 和 engine report 路径/hash。
- 每个不可变 UnitSet 发布 `engine.json`，保存完整有效配置、文档画像、策略尝试、最终选择、大小/结构统计、token audit、coverage 和诊断。validate 重验 report hash、artifact、UnitSet 身份公式、配置指纹、Unit 内容和 owned-range 覆盖。
- 新增轻量 `TokenCounter` 接口与 `off/audit/hard` 行为。hard 模式要求 tokenizer 指纹匹配并在普通文本内执行 token-aware split；未注入 counter 的 audit 明确为 unavailable。CLI `audit-tokens` 提供具名 Unicode-codepoint 诊断 counter，不冒充模型 tokenizer。
- 共享运行时版本为 `0.2.1`，仍为零运行时第三方依赖；完整源码生成到 bootstrap、lint、controlled-ingest 三个 Skill 的 `lib/`，复制部署和 `python -I -S` 路径保持成立。

## 验证

- `python -m pytest -q`：`212 passed`。
- Chunk Engine 专项覆盖可靠 outline、中文 heuristic、碎片回退、fenced-code heuristic 隔离、oversized 保护、hard token split、counter fingerprint 和 unavailable audit。
- 文件式运行覆盖 preview/build 一致性、UnitSet v2/engine report 发布、确定性 ID、精确回读、context、CLI token audit、report/source 篡改、revision 冲突、Bundle 资产和 QA 引用。
- `sync_skill_runtime.py --check` 验证三个 Skill 内嵌副本与 canonical 源码一致。

## 阶段边界

P2.1 决定共享来源内容怎样切，P3 决定 Wiki 怎样消费和引用，P5 决定怎样渲染、embedding、索引和召回。当前 qmd-like-rag 仍使用旧 Markdown chunker；真实 embedding tokenizer audit、普通 Unit 与索引文档一一对应，以及删除新链路中的 `chunk_markdown_file()` 调用是 P5 验收，不在本阶段伪称完成。

P2 的 UnitSet v1 是开发期产物，不提供迁移器。后续 P3 fixture 和全新实践库只能使用 P2.1 UnitSet v2。
