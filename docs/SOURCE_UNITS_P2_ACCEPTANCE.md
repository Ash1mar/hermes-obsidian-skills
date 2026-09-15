# P2：来源内容层验收

> 这是 P2 基础实现的历史验收。P2.1 已用 UnitSet v2 和 `engine.json` 取代这里记录的 UnitSet v1/`diagnostics.json` 发布格式；最新门禁见 ADR-0005 和 P2.1 验收记录。

日期：2026-09-15。范围：本地 `main` 实现；不迁移旧内容，不创建正式 Vault，不接入知识构建或 Provider。

## 交付

- `hermes-source-units` 升级为 `0.2.0`，新增零第三方依赖的 `FileSourceUnitService`。开发源码保持一份，生成到 bootstrap、lint 和 controlled-ingest 的完整 Skill 内置副本。
- controlled-ingest 新增 `manage_source_units.py`，提供 `prepare-markdown`、`prepare-bundle`、`preview`、`build`、`list`、`validate`、`get` 和 `context`。
- Markdown 与 governed Bundle v2 可生成不可变规范产物。正文固定为 UTF-8/LF，artifact revision 覆盖来源 hash、规范正文、outline 和资产。
- P2 优先使用可靠 outline；普通 Markdown 识别围栏外标题；无结构时识别编号、分页或分隔线，再回退递归切分。代码围栏、块公式、Markdown 表格和列表保持结构边界；超出上限的受保护结构完整保留并写入诊断。
- Section scope 扣除直接子 scope 后形成非重叠 owned ranges。SourceUnit 是知识构建与未来 RAG 共用的 canonical chunk，默认 target/max/overlap 为 512/1024/80 codepoints；按范围并集覆盖完整规范正文，overlap 不跨 owned range。坐标为 Unicode codepoint、0-based 半开区间；标题路径和读取上下文不混入核心 hash。
- UnitSet 使用 P0 公式生成确定性 ID，保存为 `_system/sources/units/<resource>/<unit-set>/{manifest.json,sections.json,diagnostics.json,units.jsonl}`，原子更新 `current.json`。expected revision、发布锁和不可变历史目录阻止覆盖及并发混代。
- 文本和 Bundle 资产均可成为 Unit。Bundle 章节 QA 和 oversized 诊断通过 `quality_refs` 指回同一 UnitSet 的 `diagnostics.json`；未知页码或区域保持未知。
- `get` 按完整 UnitRef 和可选子范围精确回读，`context` 在当前 section 及祖先 section 中按 codepoint 预算扩展，不进入兄弟子章节，并分别返回 core、context 和 omitted refs。读取重验产物/Unit hash、registry revision、来源处理状态；query purpose 额外要求 active version 和 approved source organization。
- lint 新增只读 SourceUnit 仓库校验和数量指标。P3/P4/P5 尚未实现时继续报告 `source_units.pipeline_pending`，不会把 P2 Vault 宣称为 query-ready。

## 验证

隔离测试把完整 ingest Skill 复制到临时目录，在 `python -I -S` 下从临时 bootstrap Vault 运行。覆盖 CRLF、中文/emoji、Markdown 标题、编号标题启发式、配置化 overlap、父子 outline 自有范围、表格资产、Bundle QA、超大围栏、递归切分、preview/build 一致、确定性 ID、幂等重发、revision 冲突、精确回读、上下文截断、registry 冲突、正文篡改和 lint 损坏报告。

共享副本 `sync_skill_runtime.py --check` 通过；新 Skill 入口以 Git `100755` 登记。完整仓库回归为 `204 passed`。

## 阶段边界

P2 已让通用 Unit 在文件后端上独立成立，数据库不再是 ID、定位或引用成立的前提。它也是下游共同使用的 canonical chunk，而不是 RAG 之前还要普遍重切一次的中间来源段。P2 不生成 Wiki 页面，不执行 Pass/Reduce，不提交 knowledge build，不做 Vault Finalize，也不向 Chroma/BM25 投影。后续按 [ADR-0004](architecture/0004-knowledge-identity-and-finalize.md) 进入 P3：稳定 subject/page identity、Pass 0、Pass 1..N、Reduce 与 Build Finalize。
