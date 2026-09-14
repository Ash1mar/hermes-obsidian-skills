# Wiki 与通用知识构建：WeKnora 实现及 Hermes 当前流程详解

本文是 [ADR-0002](architecture/0002-general-knowledge-construction.md) 的实现说明，不替代 ADR，也不新增运行授权。

- 核对日期：2026-09-10。
- 本仓库核对基线：`a74b631437541129b61dccb44f1101eab59dc567`。
- WeKnora 核对对象：腾讯官方仓库当日可读取的 `main` 源码，不是固定发布包，也不是此前讨论的 0.6。本文不能用于断言某项功能在哪个历史版本首次出现。
- 本文中的“代码约束”表示程序能检查或执行；“模型规则”表示执行 Skill 的模型必须遵守，但程序未必能证明其遵守；“建议”不表示已经实现。

## 1. 先明确：生成 Wiki 不是把 PDF 转成 Markdown

这里至少有四项不同的工作：

| 工作 | 回答的问题 | 我们的主要落点 |
|---|---|---|
| 来源准备 | 原件是什么，解析出了哪些内容？ | 原件、Bundle、转换校验 |
| 知识构建 | 内容涉及哪些对象，应该形成什么知识页面？ | 候选判断、页面、构建记录 |
| 身份与版本治理 | 谁提供，属于哪个逻辑文档，哪个业务版次可用？ | 文档和来源机构 registry |
| 检索发布 | 哪些材料及其派生页面能进入本次查询？ | Provider、索引状态、controlled-query |

本仓库继续使用 Vault 作为受控文件集合的名称。引入治理并没有要求废弃这个名称，也没有把所有 Markdown 自动升级为正式知识。

本文的核心比较是：WeKnora 把 Wiki 构建做成应用服务；我们把它做成 **Skill 驱动的内容判断，加脚本约束的证据和状态闭环**。两边都有模型参与，并不是一边“有规则”、另一边“纯 AI 自由发挥”。

## 2. WeKnora：从文档到 Wiki 的实际路径

### 2.1 页面、配置与图不是同一种对象

WeKnora 的 Wiki 页面有独立身份、slug、正文、别名、状态和来源引用。自动摄取主要涉及文档摘要、实体、概念、索引；类型定义中另有 synthesis、comparison，不能因此断言每次摄取都会生成综合和比较页。页面修订也不等于原始文档的业务版次。

内容相关配置包括 `extraction_granularity`、`extraction_instructions`、`content_instructions`。抽取粒度提供 standard、focused、exhaustive；抽取指导影响选什么，内容指导影响怎么写。这些可选指导为空时，系统仍能依靠内置规则运行，不要求业务人员预先枚举全部对象或页面。模型、批量和并发参数属于运行配置，不是领域知识。

依据：[Wiki 页面及 WikiConfig 类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/wiki_page.go)。

另需区分：Wiki 页面之间的链接形成导航关系；结构化图谱抽取配置属于另一条能力线。不能把页面链接图等同于“每条关系已通过领域本体验证”。知识库另有抽取配置入口，不能拿它替代 WikiConfig 的含义。依据：[知识库类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/knowledgebase.go)。

### 2.2 构建顺序

```text
解析后的文档与 chunks
  → 待处理操作
  → Map：候选页面识别
      ├─ 文档摘要
      └─ 候选与 chunk 的关联
  → 汇总到页面 slug
  → Reduce：结合旧页和来源变化更新页面
  → Finalize：索引与链接整理
```

主调用入口为 `ProcessWikiIngest`；逐文档工作在 `mapOneDocument`，逐页面工作在 `reduceSlugUpdates`，全库收尾另有 `ProcessWikiFinalize`。因此不能仅看到一个抽取 Prompt，就把整个流程理解成“一次调用生成全部 Wiki”。

正常路径先识别轻量候选，再并行生成摘要和关联 chunk。chunk 关联还能发现新候选。同一页面的多份来源汇聚后更新，而不是每份材料各自生成一份同名页面。

重要例外：候选提取失败可退回旧式抽取；未取得引用的候选也可能保留描述性回退内容。摘要本身使用文档级引用。摘要生成失败会触发失败重试，而非正常宣告该文档全部完成。不能将这套实现描述成“所有页面均强制具备 chunk 级证据”。

依据：[批量构建实现](https://github.com/Tencent/WeKnora/blob/main/internal/application/service/wiki_ingest_batch.go)。

### 2.3 每类模型调用承担什么规则

下面按职责概括内置规则，不复制整套提示词；提示词本身不是事实正确性的证明。

| 提示职责 | 主要规则 |
|---|---|
| 候选发现 | 识别实体和概念，维护名称、slug、别名；相关对象不等于同一对象 |
| 引用关联 | 从给定 chunk 中找实质支持，只使用给定 ID；不能凭空造证据 |
| 文档摘要 | 依据正文，不把文件名当事实；引用合法页面和原始图片地址 |
| 页面修改 | 新事实依赖来源内容；摘要用于上下文，不替代证据；避免把邻近对象信息写进当前对象 |
| 冲突与撤回 | 明确替代才改写；不确定冲突保留说明；撤回来源时清理失去支持的内容 |
| 同一性去重 | 在允许的候选目标中判断同一对象，不因为语义相关就合并 |
| 分类及索引 | 按对象是什么归类，复用稳定标签；索引介绍不代替实体正文 |

依据：[Wiki 内置提示规则](https://github.com/Tencent/WeKnora/blob/main/internal/agent/prompts_wiki.go)，对应 `WikiCandidateSlugPrompt`、`WikiChunkCitationPrompt`、`WikiSummaryPrompt`、`WikiPageModifySystemPrompt` 等符号。

### 2.4 模型之外的服务责任

服务负责排队、失败重试、引用处理、去重结果检查、页面写入和链接修整。重新解析或删除来源时，会产生相应增补和撤回操作；这是来源依赖维护，不等于业务上批准新版本。链接注入也有代码过滤，不能任意向归档页、代码片段等位置补链接。

这些能力仍有运行边界：并发锁、数据库和模型调用可能失败；不是跨全部页面的无限重试或全局事务保证。应观察任务及页面状态，而不是只看某页存在。

依据：[Wiki 摄取辅助服务](https://github.com/Tencent/WeKnora/blob/main/internal/application/service/wiki_ingest.go)。

### 2.5 对我们的启发，以及不能直接照搬的推论

从上述实现可以提炼出五个适合我们采用的设计原则：

1. 先识别对象、再组织证据、最后写页，避免“看到一段就立即建页”。
2. 同一性判断和正文生成分离，避免写得越多重复越多。
3. 摘要是理解辅助，不应成为最终事实的唯一证据。
4. 更新时考虑旧内容和失效来源，而不是不断追加新段落。
5. 页面正文、页面来源和全库导航分别维护。

这是本文的架构归纳，不代表我们复制了其调度器、数据库或全部模型调用。尤其不能照搬“遇到引用问题用描述回退”的策略来绕过本仓库的证据门禁。

## 3. 我们的实现由谁负责

| 组件 | 负责 | 不负责 |
|---|---|---|
| bootstrap | 创建目录、规则、模板和可选治理骨架 | 自动读完材料、批准业务版次、替每份材料生成全部知识 |
| controlled-ingest 的模型执行者 | 读来源、发现候选、判断身份、选择动作、写正文、复核 | 绕过身份冲突、伪造哈希、批准未经授权的来源 |
| Governance Manager | registry 的受支持写入、修订冲突检查、登记及激活状态转换 | 替模型抽取知识、证明每句业务结论正确 |
| Bundle Manager | 章节范围、领取与完成状态、source-map、输出关联 | 根据输出文件存在自动判定内容已读完 |
| knowledge-build 校验器 | 检查构建记录、证据范围、输出及复核一致性 | 调用 LLM、自动写正文、证明语义蕴含 |
| provenance 同步器 | 把构建证据投影到页面和各支持章节 ledger | 执行业务审批、改变章节完成状态 |
| vault-lint | 独立只读审计 | 自动修复所有问题、替代上述写入入口 |
| Provider / query | 建索引及执行查询可见性策略 | 把一次入索引当作知识构建完成 |

具体入口见 [bootstrap Skill](../hermes-obsidian-vault-bootstrap/SKILL.md)、[ingest Skill](../hermes-obsidian-controlled-ingest/SKILL.md)、[治理命令说明](../hermes-obsidian-controlled-ingest/references/document-governance.md)、[lint Skill](../hermes-obsidian-vault-lint/SKILL.md)。

目前没有一个 Python“总按钮”自动完成所有 LLM 阶段。执行者需要遵照 Skill 编排以下过程；脚本验证其中可机械检查的部分。

## 4. 第一层：来源准备，不是知识完成

### 4.1 明确本次任务边界

先区分用户授权的是来源准备、知识构建，还是两者都要：

- 只要求转换时，完成 Bundle 可以停止，报告“来源已准备”。
- 已要求知识构建时，不能转换完就声称 ingest 全部结束。
- 已存在一致、可用的 Bundle 时，优先继续读取和构建，不必重新跑全部 MinerU。
- 多个独立来源中局部失败时，记录失败范围，继续获授权且不受影响的工作。

这是执行规则，不是校验器自动识别用户意图的功能。

### 4.2 原件、身份、解析产物各保留什么

原件保留在 `10_Raw/`，不得把生成知识写回原件。启用治理时，`ingest-start` 登记明确的逻辑文档、版本、资源和来源身份，并计算文件指纹；完成转换后 `ingest-finish` 检查 Bundle 与原件哈希的一致性，并投影治理身份。

材料身份清单回答“是什么材料、由谁提供、属于哪个版本”，不是“业务人员希望重点讲哪些主题”。业务重点及写作偏好可以缺省；身份未知时应保留不确定性，不能把文件夹名称或 LLM 推断冒充已批准的业务身份。

Bundle v2 的分工为：

| 文件/数据 | 用途 |
|---|---|
| `manifest.json` | 来源指纹、转换信息、资产及治理投影等控制信息 |
| `outline.json` | 章节结构和定位信息 |
| `document.md` | 供读取的规范化正文 |
| 关联图片、表格资产 | 需要时读取的具体证据 |
| `_evidence/` | 针对具体 QA 问题回查的解析证据，不是默认递归摄取对象 |

MinerU 负责解析，Bundle 负责组织解析结果；二者都不承担对象合并、知识页写作和业务版本激活。

### 4.3 用章节 ledger 划分阅读责任

Bundle Manager 的 `init` 创建或协调 `_system/reports/` 下的 source-map 和 section-ledger。ledger 才是章节状态权威；source-map 是可读投影，不能手工改它来冒充状态变化。

父章节的完整范围可能包含子章节。实际摄取使用 `content_ranges`：从父范围扣除子范围后，得到不重叠的自有文本范围。这样可以逐章推进，也不会父子重复摄取。

普通工作先以当前 `revision` 领取章节为 `in_progress`，再读对应正文和关联资产。不要重新抢占其他运行尚未结束的章节。变更后协调会识别 `stale`，保留旧输出供复查，不自动删掉知识页。

完整命令与状态规则见 [Bundle ledger 说明](../hermes-obsidian-controlled-ingest/references/bundle-source-map-ledger.md)。

## 5. 第二层：当前 v3 通用知识构建

### 5.1 读取并记录 inspected_ranges

执行者实际读取选定范围，再记录：

| 字段 | 意义 |
|---|---|
| `path` | Vault 内、正斜杠形式的来源相对路径 |
| `sha256` | 当前来源文本文件的 SHA-256；不要与原件 SHA 混淆 |
| `lines: [起, 止]` | 包含两端的实际阅读范围 |
| `qa`、`reason` | 可用性及为何读取、发现了什么 |
| `qa_note` | v3 中受 QA 限制范围必须说明具体问题 |
| `ledger_path`、`ledger_revision` | Bundle 对应章节 ledger 及观察到的修订 |
| `bundle_id`、`section_id` | 该阅读范围属于哪个 Bundle、哪个章节 |

代码检查路径、文本指纹、行范围，以及 Bundle/ledger/章节之间的关系。一次记录的范围必须包含在相应章节的一段合法 `content_ranges` 中；多个分离范围分别记录。

代码只能证明“声明的范围存在且一致”，不能证明模型真正阅读或理解了它。不能用复制一组行号代替阅读。

### 5.2 提取候选，不立即生成页面

候选 `kind` 有五类，它们是通用内容角色，不是某个专业的完整本体：

| kind | 识别目标 | 常见处理倾向，非自动路由规则 |
|---|---|---|
| `entity` | 可区分的具体对象 | 合并到已有对象说明或新建有来源的卡片 |
| `concept` | 可以跨材料解释和复用的概念 | 先查概念目录，遵守概念治理 |
| `requirement` | 对某范围施加的要求或约束 | 保留主体、条件、适用范围和出处 |
| `fact` | 来源明确陈述的事实 | 进入适当知识页，避免每句话建一页 |
| `analysis` | 基于证据的比较或推导 | 明确标为分析，并填写推导依据 |

不要求先提供“全部系统清单”才开始；也不把输入文件夹自动当作本体类别。文件夹可以提供导航语境，不能替代正文事实。

### 5.3 判断同一性：是不是同一个对象

模型查看已有目标，比较名称、别名、定义、项目、适用范围及业务版本。`identity_rationale` 记录判断理由，`existing_targets` 记录检查过的现有页面。

必须区分：

- 同一对象的不同称呼：可以复用或更新。
- 相互关联的两个对象：应链接，不应合并。
- 不同项目中的同名对象：不能仅凭名称归并。
- 同一逻辑文档的两份业务版次：应保留来源版本差异，不把后到达当作更权威。
- 无法确定身份：推迟判断，或保持明确分离的草稿，不猜测稳定同一性。

这是模型判断规则。校验器要求理由和目标，但不会自动运行领域实体消歧模型来证明理由正确。

### 5.4 给候选选择明确动作

| decision | 意义 | 代码检查要点 |
|---|---|---|
| `create` | 创建有独立价值的新输出 | plan 时目标不能已经存在；完成时必须存在 |
| `update` | 修改现有页面 | 目标存在，且列在检查过的现有目标中 |
| `reuse` | 已有页面足够，不重复建页 | 目标存在，且列在现有目标中；仍维护本次证据关联 |
| `relate` | 保持对象独立并补关系 | 要有现有目标和输出，不能冒充同一对象合并 |
| `defer` | 证据或身份问题尚不能解决 | 记录原因，不宣称已产出知识 |
| `skip` | 经检查决定不构建 | 记录原因，不宣称已产出知识 |

所有候选需要唯一 `id`、名称、身份理由、动作和原因。前四种动作必须带证据及输出；`analysis` 的生产性动作另需 `derivation`。零候选也不是自由通过：需要可验证的检查范围及 `empty_reason`。

候选 ID 用于本次构建记录，不自动等于跨运行稳定实体 ID；输出路径也不是未来通用本体对象注册表的替代品。

### 5.5 证据绑定和 QA 的当前边界

每条候选证据包含来源路径、指纹、行范围和 QA 状态，并必须被本次检查范围覆盖。不能仅以候选摘要、旧回答或检索命中摘要作事实证据。

当前 v3 允许受限证据形成**带具体 QA 说明的草稿**：

1. inspection 和 evidence 明确保留 `needs-qa`，不能改标为 `usable`。
2. 填写具体 `qa_note`，说明究竟是表头、公式、读序还是其他问题。
3. 受影响输出必须为 `status: draft`。
4. QA 说明进入页面来源投影，并登记到对应章节的 `qa_items`。
5. 完成时对应支持章节可为 `qa_required`，不伪装成全部无问题。

这不是允许使用解析失败或身份不一致的内容。模型仍须判断哪部分可归属于来源、哪部分不能下结论。例如“来源列出该指标，但单位待核验”与“该指标确定为某数值和单位”是不同陈述。

依据：[当前校验器](../hermes-obsidian-controlled-ingest/scripts/validate_knowledge_build.py)。旧 v1/v2 的生产性证据限制更严格，不能用旧示例说明 v3 的全部行为。

### 5.6 写读者需要的页面，而不是处理日志

模型根据候选及证据写正文：先回答对象是什么、有什么可靠信息、适用范围和相互关系，再提供出处及局限。不要把“调用过哪些工具、跑了几次转换”写成知识主体；处理日志放报告或 ledger。

页面落点应服从现有模板和规则：

- `20_Notes`：适当的整理说明。
- `30_Cards`：有边界的知识单元。
- `40_Concepts`：受概念目录和审批约束的概念页，不因模型识别出 concept 就任意新增。
- `50_Projects`：真正有具体项目依据的页面，不把任意主题索引伪装成项目。
- `_system/reports`：来源、构建和 QA 等报告；不能用报告数量代替知识产出。

这些目录是校验允许的输出范围，不是“entity 必须映射到某目录”的硬编码函数。

索引页还需避免权威性误导。v3 对 `evidence_mode: index` 检查 `evidence_scope` 与证据来源路径数量匹配，`evidence_coverage` 为 `complete` 或 `representative`，`evidence_authority` 为 `navigation`。这里的“complete”是记录的覆盖声明，程序不证明语义上的穷尽。

### 5.7 页面写好后，还要复核并同步来源

v3 新增的闭环是：

```text
写正文
  → provenance 预览，取得 authored_sha256
  → 模型对照证据复核正文，填写 output_reviews
  → apply：同步页面来源块和所有支持章节
  → 逐章节确认终态
  → execution_status = completed
  → complete 校验
```

`output_reviews` 每个输出记录 `path`、`authored_sha256`、`finding`；项目输出另需 `project_basis`。脚本没有调用另一个 LLM 自动审核，复核由执行者完成；也不要求每一次都新增一次人工审批。

`authored_sha256` 针对排除工具拥有的 provenance 块之后的页面内容计算，包含其余正文及 frontmatter。正文变更后旧复核失效，需重新预览和复核；自动更新来源块不会单独使正文复核失效。

同步器把同一输出的所有候选证据取并集，生成 `knowledge-provenance:start/end` 标记内的“来源与核验状态”块，并把该输出登记到**每一个**支持它的 Bundle 章节。不能只登记最早来源、已激活来源，或本次主文件。

重要限制：这个并集是页面级证据集合，尚不是逐句 Claim—Evidence 的形式化映射。正文仍需要明确归属和局部引用；JSON 块不能替代良好写作。

依据：[来源同步器](../hermes-obsidian-controlled-ingest/scripts/sync_knowledge_provenance.py)及[构建校验器](../hermes-obsidian-controlled-ingest/scripts/validate_knowledge_build.py)。

## 6. 当前可执行的操作顺序

以下命令是 **WSL/Linux 参数模板**。`<...>` 必须替换为真实路径；并非可直接执行的完整任务脚本。来源准备、候选记录和模型写页步骤不能省略。

### 6.1 写入前

1. 核对任务授权；有治理的 Vault 先处理所需登记及 Bundle 一致性。
2. `manage_bundle_ingest.py init` 协调 ledger；用最新修订领取待处理章节。
3. 阅读正文、资产及现有目标。
4. 在 `_system/reports/<run>.knowledge-build.json` 建立 `hermes-knowledge-build/v3` 记录，填实际 inspection、候选和拟输出；执行状态保持 `in_progress`。
5. 在 create 输出还不存在时检查计划：

```bash
python3 "<ingest-skill-root>/scripts/validate_knowledge_build.py" \
  "<vault>/_system/reports/<run>.knowledge-build.json" \
  --vault "<vault>" --phase plan --require-current
```

`plan` 不是干跑整套 Wiki 引擎；它只检查记录。创建完页面后再次使用同一个 create 计划跑 plan 会报告目标已存在，应进入后续校验，而不是为了通过改写历史。

### 6.2 写入与最终复核

6. 按记录写入或修改页面。检查内容归属、证据、QA、项目依据及索引属性。
7. 预览来源同步：

```bash
python3 "<ingest-skill-root>/scripts/sync_knowledge_provenance.py" \
  "<vault>/_system/reports/<run>.knowledge-build.json" --vault "<vault>"
```

预览不写页面及 ledger，返回 `review_inputs` 等信息。它会读取实际输出，所以不能在拟创建页尚不存在时运行。

8. 模型实际复核页面，将返回的指纹及复核结论填入 `output_reviews`。不是只复制哈希而不检查内容。
9. 应用来源投影：

```bash
python3 "<ingest-skill-root>/scripts/sync_knowledge_provenance.py" \
  "<vault>/_system/reports/<run>.knowledge-build.json" --vault "<vault>" --apply
```

同步器内部使用 `prepare`、`written` 校验阶段；这两个名称**不是** `validate_knowledge_build.py --phase` 的 CLI 选项。CLI 仅支持 `plan` 和 `complete`。

### 6.3 收尾

10. 重新读取各 ledger 最新修订，用 Bundle Manager 确认实际完成、明确跳过或待 QA 的章节终态。同步器已登记输出，但不会替执行者认定阅读完成。
11. 确认本次记录所述工作确实完成后设置 `execution_status: completed`。
12. 最终校验：

```bash
python3 "<ingest-skill-root>/scripts/validate_knowledge_build.py" \
  "<vault>/_system/reports/<run>.knowledge-build.json" \
  --vault "<vault>" --phase complete --require-current
```

13. 运行 Vault lint；如用户授权，再进行 Provider 同步及查询验证。最终报告分别列出转换、章节、知识输出、QA、治理状态和检索可见性。

一份局部构建记录通过 complete，不等于全库全部章节完成。v3 可以完整记录一个仍带 QA 的构建结果；整个 Bundle ledger 的 complete 条件仍要求其有效章节全部 ingested 或 skipped，不能混为一谈。

### 6.4 出错后怎样恢复

| 问题 | 正确处理 |
|---|---|
| 原文指纹改变 | 协调 Bundle/ledger，重读受影响内容，更新真实检查记录，不只换一个哈希 |
| ledger 修订冲突 | 重读状态，确认其他运行的变化后再操作 |
| 页面在复核后改变 | 重新预览、复核，再 apply |
| Windows/WSL source-map 路径不匹配 | 在实际执行环境协调 ledger；不要绕过路径安全检查 |
| 同步中途失败 | 检查已写入部分，排除原因后重跑；不要声称自动全局回滚 |
| 一个来源待 QA | 标注受影响范围和草稿，按权限继续独立可靠范围 |
| 没有检索结果 | 先区分未构建、未同步、治理不可见及召回失败 |

来源同步具有避免重复追加的设计，页面单文件替换具有原子性，但跨多个 ledger 和页面不是一个数据库事务。部分成功后需要核查、重跑和最终校验。

## 7. 治理状态为什么不能合并成一个“完成”

| 状态/事件 | 表示什么 | 不表示什么 |
|---|---|---|
| Bundle 校验通过或带警告 | 解析产物满足相应结构条件 | 所有内容都正确、已生成 Wiki |
| `processing_status: completed` | 来源处理完成 | 业务版本已激活 |
| 构建记录 completed 且校验通过 | 本次声明范围已完成相应构建闭环 | 全库无遗漏、结论经专家审定 |
| 页面 `status: draft` | 内容尚为草稿或受限结果 | 单靠这个字段就实现访问隔离 |
| 来源机构 approved / 版本 active | 治理状态满足相应条件 | 每句话都经逐项专家审核 |
| Provider 同步成功 | 相应可见数据已进入索引流程 | 排名、召回和答案一定正确 |

经授权可读取的一致且处理成功的候选/未激活来源，并不因“未激活”单独阻断草稿构建。实际访问限制、身份不一致、解析失败仍会阻断受影响证据。

派生页必须完整关联所有支持来源。现有治理查询策略会依据这些来源链路判断资格；混合了不合格来源的页面不能只靠另一个合格来源取得可见性。不得复制到未登记页面来绕过门禁。

规则详见 [document-governance.md](../hermes-obsidian-controlled-ingest/references/document-governance.md)。

## 8. 一个跨来源例子：不是照文件夹各写一份

以下仅为虚构流程示例，不是对任何实际材料的判断。

材料 A 是项目甲某设备的说明，材料 B 是该设备的后续版说明，材料 C 是项目乙的同名设备材料。

1. **准备**：三份材料分别保留原件与解析结果。A/B 是否属于同一逻辑文档及不同业务版次，需要有来源依据；C 不因同名就成为同一个对象。
2. **发现**：从具体章节发现设备对象、技术概念、约束和事实。并不强制每类都生成一页。
3. **同一性**：A/B 的共同对象可更新现有页；C 保留项目乙语境。共用概念可以复用现有概念页，但不混合项目参数。
4. **证据**：对每条用于写作的范围登记真实指纹和行号；B 的疑似表格误读单独 needs-qa。
5. **写作**：清楚区分“A 版陈述”和“B 版新增且待核验的内容”，不自动把 B 当正式替代。不用待 QA 内容覆盖既有批准结论。
6. **综合**：有需要时生成跨来源主题索引或分析，标明覆盖范围；不要把综合主题当真实项目。
7. **登记**：同页依赖 A/B 时两边支持章节都登记输出；页面来源块汇总全部支持证据。
8. **完成**：页面复核、来源同步、章节终态和最终校验完成后，报告草稿与 QA 情况。业务激活另走治理流程。

这个例子体现我们对借鉴思路的补充：通用发现可以自动开始，但不能省略项目区分、证据限制和业务版本边界。

## 9. 两套流程的逐项差异

下表是对前述源码和本仓库规则的归纳，不是产品完整能力排名。

| 维度 | WeKnora 本文所查路径 | 本仓库当前实现 |
|---|---|---|
| 执行主体 | 应用服务调用模型并调度任务 | Skill 执行者调用工具、写文件 |
| 起点 | 已解析文档/chunks | 原件准备后按 Bundle 章节读取 |
| 内容选择 | 内置通用规则，可叠加可选指导 | 通用候选规则、已有规则文件和模板 |
| 知识身份 | 页面 slug、别名及去重 | 检查现有页面，记录 identity_rationale 与动作 |
| 默认产物 | 摘要、实体、概念、索引等 | Notes、Cards、Concepts、Projects 和治理报告 |
| 证据粒度 | 文档/部分页面 chunk 引用，存在回退路径 | 文件指纹、行范围、Bundle 章节及 QA |
| 页面更新 | 服务将来源增减汇总后更新 | 执行者重读、判断、更新并校验 |
| 导航维护 | 服务收尾整理索引和链接 | 执行者依规则维护，lint 审计；非同等自动服务 |
| 内容复核 | 模型生成及服务处理 | v3 显式页面复核记录绑定正文指纹 |
| 来源变化 | 有增补/撤回任务路径 | ledger stale/orphaned 检测，输出保留供复查 |
| 业务治理 | 不能由 Wiki 状态推定其满足我们的业务规则 | registry、来源机构、逻辑文档、版次及查询门禁分离 |
| 自动化程度 | 连续服务管线 | 有可执行工具，但整套语义流程仍需 agent 编排 |

我们并没有实现“精简版 WeKnora 的所有能力”。目前更准确的定位是：**一条可继续自动化的、证据受控的文件式知识构建流程**。

## 10. 当前实现的保证与不足

### 10.1 已可机械检查的内容

- 相对路径与安全边界、输出目录和扩展名。
- 来源存在、文本指纹匹配及行范围合法。
- 证据被声明的阅读范围覆盖。
- Bundle 身份、ledger 和章节关联一致。
- create/update/reuse 等动作的部分结构条件。
- needs-qa 不被洗成 usable，受限输出维持 draft。
- 完成时每个支持章节登记全部相应输出及 QA。
- 正文复核指纹未失效，页面 provenance 与候选证据并集一致。
- 索引属性、项目依据等必要字段存在。

### 10.2 仍然依赖执行者判断的内容

- 阅读是否充分、候选是否漏掉重要知识。
- 两个名称是否真正同一对象。
- 引用是否足以支持具体句子。
- QA 范围和结论是否判断正确。
- 推导是否有效、是否错误推广到其他系统或项目。
- 新内容是否真的应覆盖旧内容。
- 页面是否清楚、克制、有独立使用价值。

`finding` 或 `derivation` 非空只能证明填写了说明，不证明说明正确。复核记录也不等于独立专家审批。

### 10.3 尚不能宣传为已实现的功能

- 可装载多领域本体并硬校验所有实体、属性、关系。
- 业务人员可编辑的统一内容配置及示例库完整产品界面。
- 稳定、独立于页面路径的通用知识对象注册系统。
- 来源变化后的全库知识自动重写、自动撤回及全局事务。
- 每个正文 Claim 的机器可验证蕴含关系。
- 自动保证全部有价值知识被抽取。

这些可以继续设计，但不应写进“当前流程已保证”的范围。

## 11. 特别注意：现有说明与代码有版本差异

本次检查发现：

| 落点 | 核对到的现状 | 阅读建议 |
|---|---|---|
| ADR-0002 | 新工作仍写 v2 | 理解决策来源，但不要照抄旧 contract |
| `references/knowledge-construction.md` | 主要说明 v2 | 流程思想仍有价值；v3 命令顺序看本文及代码 |
| ingest Skill | 仍有 v2 文字，同时已有 QA 草稿恢复规则 | 不把旧字样解释为可绕过 require-current |
| 校验器、来源同步器及测试 | 当前 contract 是 v3 | 新工作以当前脚本接受的契约为准 |
| 部分 QA/综合写作说明 | 仍保留较严格的旧描述 | 区分可归属的受限草稿、正式结论与不可用证据，不扩大许可 |

尤其是 `--require-current`：当前要求 v3，绝不是“只要不是 v1 就行”。历史 v1/v2 记录仍有兼容入口，但兼容读取不代表新工作应继续生成旧格式。

本文没有顺手改动这些 Skill、ADR 或运行代码；它把不一致公开列出，避免在说明中虚构已经完成的一致化升级。正式统一规则措辞应作为单独维护工作。

另一个实际限制：lint 检查已有的构建记录，不会因为老 Vault 缺少这种记录就一概报错。旧库可兼容使用，不能因此推断旧页面都已经具备 v3 来源闭环。

## 12. 维护和核验入口

| 想确认什么 | 优先查看 |
|---|---|
| 为什么借鉴这些思路 | [ADR-0002](architecture/0002-general-knowledge-construction.md) |
| agent 应怎样摄取及写作 | [controlled-ingest/SKILL.md](../hermes-obsidian-controlled-ingest/SKILL.md) |
| 旧版候选和构建说明 | [knowledge-construction.md](../hermes-obsidian-controlled-ingest/references/knowledge-construction.md) |
| 当前记录实际接受什么 | [validate_knowledge_build.py](../hermes-obsidian-controlled-ingest/scripts/validate_knowledge_build.py) |
| 当前来源投影怎样写 | [sync_knowledge_provenance.py](../hermes-obsidian-controlled-ingest/scripts/sync_knowledge_provenance.py) |
| 原件身份与业务版次 | [document-governance.md](../hermes-obsidian-controlled-ingest/references/document-governance.md) |
| 章节状态与范围 | [bundle-source-map-ledger.md](../hermes-obsidian-controlled-ingest/references/bundle-source-map-ledger.md) |
| 行为回归用例 | [test_knowledge_construction.py](../tests/test_knowledge_construction.py) |

本次对说明涉及的构建回归执行了 `python -m pytest tests/test_knowledge_construction.py -q`：25 项通过。这证明相关测试用例通过，不代表真实材料的知识质量或 WeKnora 部署已做端到端验收。

后续更新本文件时，应重新核对上游调用路径、本地 `CONTRACT`、脚本 CLI 和测试；不要只更新示意图而忽略失败分支、版本兼容及实际写入顺序。
