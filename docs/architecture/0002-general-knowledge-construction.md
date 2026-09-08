# ADR-0002：通用知识构建流程——借鉴 WeKnora，融合证据与版本治理

## 状态与范围

2026-09-08 接受。实现范围为受控 ingest 的通用方法、构建决策记录、只读校验、
Vault lint 接入及新库模板。它不是自动 LLM 服务或完整本体推理器；语义判断仍由执行
Skill 的模型与校审人员承担。真实材料的生成效果尚须试点验证。

不涉及模型配置、并行度、批次调度、数据库、Provider、Bundle v3、实际 Vault 迁移，
也不将河北材料的候选系统或预分配文档 ID 当作领域事实。

## 背景

既有流程擅长原件保护、Bundle v2、QA、section ledger、文档版本控制及保守概念治理，
但从材料分类直接跳到卡片/概念/索引，缺少显式的候选知识与身份决策层。
具体对象不是抽象概念，禁止概念膨胀不应阻止对象识别。当前模板也把部分摄取审计
内容混入读者正文。业务人员不应先填写完系统清单，通用默认流程必须能独立运行。

## 借鉴来源与取舍

研究依据为 2026-09-08 查阅的 WeKnora 公开 main（链接会随上游更新，未固定发行版）：

- [Wiki prompts](https://github.com/Tencent/WeKnora/blob/main/internal/agent/prompts_wiki.go)
- [Wiki pipeline](https://github.com/Tencent/WeKnora/blob/main/internal/application/service/wiki_ingest.go)
- [Wiki types](https://github.com/Tencent/WeKnora/blob/main/internal/types/wiki_page.go)

借鉴其分阶段处理、对象/概念区分、身份延续与证据关联思想；不复制提示词全文。
我们保留更适合现有工程材料的 Bundle 定位、QA 排除、业务版次及人工复核边界。
WeKnora 的通用 Wiki 链接不是本项目的领域关系本体；本 ADR 也不把双链当成事实证明。

| 借鉴的方向 | 本项目的融合决策 |
|---|---|
| 候选与页面分开 | 先识别，再决定创建、更新、复用、关联、暂缓或跳过 |
| 明确对象身份 | 同名、同主题、同缩写不等于同一对象；检查项目/版次/适用范围 |
| 关联实际原文 | 使用既有 Bundle/section/page/asset，加 UTF-8 原文范围与指纹检查 |
| 页面持续维护 | 标记受影响产物并校审；不按到达时间覆盖，不自动级联删除 |
| 清晰知识正文 | 摄取理由留在日志；正文按内容选结构，不机械填满模板 |

## 决策

采用以下默认流程，不依赖业务配置：

有效材料证据 → 候选知识 → 身份核对 → 支持证据映射 → 产物决策 → 正文构建 → 链接/冲突/影响检查。

候选 kind 为 entity/concept/requirement/fact/analysis，仅是通用推理角色，不是强制
领域类型或新目录。候选识别不等于建页许可；具体对象可使用 Card/index，Concept
继续遵守既有准入规则。事实维护不扩写；分析合成明确推导、假设和证据覆盖。
未知、空内容和受 QA 影响的材料可以有明确的 defer/skip，不设置产物数量指标。

### 执行合同

- 规则入口为 controlled-ingest SKILL.md，详细方法在 references/knowledge-construction.md。
- 新执行的知识构建单元在现有 ingest log 旁保存 `<run>.knowledge-build.json`。
  记录候选、检查过的目标、身份理由、决策、输出路径和支持证据，不替代任何 registry。
- `validate_knowledge_build.py --phase plan` 在写知识前检查；complete 在写后检查。
  检查受控路径、证据指纹、行范围、QA 声明与决策一致性、目标存在性；不验证语义真伪。
- 现有 ledger 继续承担章节状态、产物和恢复权威；新记录不是第二套 ingest 状态机。
- lint 只检查已经存在的新记录；缺失记录的旧库不报错，不自动回填、重写或迁移。
- 新库模板提示通用流程并分开正文与日志；运行 Skill 脚本仍放在 Vault 外。

### 安全与证据边界

知识页、AI 摘要和文件名不能替代来源证据。记录引用源文本的指纹不是原件身份，
原件 SHA 与 document/version/resource 身份仍由 ADR-0001 控制。
证据变化会使记录校验失败，需审查受影响输出，不能自动换哈希“修好”。
版本、权威性、范围不明确时保留并列说明/审核项；不自动激活、合并、改名或删除。

## 后续而非本轮

业务人员可选的内容说明模板、统一内容配置入口、示例导入、正式业务对象目录、
本体约束、自动失效传播暂不实现。先验证通用流程，不以业务配置补救通用规则缺口。

## 验收

应覆盖：正常 create、计划/完成差异、已有对象复用、空候选理由、QA 拒绝、证据变化、
路径越界、行范围非法、分析缺少推导、lint 对旧库无新增要求、对新记录能发现错误。
回归 bootstrap/governance/ledger/query/lint；真实 PDF/XLSX 的知识效果需要后续独立试点。
