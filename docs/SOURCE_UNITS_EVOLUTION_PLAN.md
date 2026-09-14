# 通用来源单元：全新建库设计与实施计划

日期：2026-09-11。修订：R3，保留全新建库前提，细分实施任务并说明 P0 的已有基础、实际交付及 WeKnora 对应机制。
状态：P0 契约与 P1 bootstrap 实践已实现，P2–P6 待实施；未执行正式库重建或 ingest。P1 验收见 [P1 记录](SOURCE_UNITS_P1_ACCEPTANCE.md)。实现入口见 [ADR-0003](architecture/0003-source-unit-contracts.md)、[共享包规范](../hermes-source-units/README.md) 和 [P0 验收记录](SOURCE_UNITS_P0_ACCEPTANCE.md)。

## 1. 本轮确定的前提

按用户明确要求：先完成新体系的设计、实现和实践验证，然后重新 bootstrap，从原始材料重新 ingest。旧 Vault 的生成内容、ledger 状态、Provider chunk、索引和页面引用不进入新体系，不开发历史数据适配器。

复用已有 MinerU 接入、规范化转换、治理逻辑及可靠脚本能力，按新契约改造调用关系。代码复用不要求保留旧数据格式或旧工作流。现有分析依据为 main `a74b631437541129b61dccb44f1101eab59dc567`；本次补读 bootstrap 与 ledger 入口，未核验 WSL 的实际部署版本。

原始 PDF、Word、图片等仍是新 ingest 的输入，可以使用同一批原始材料。规范化产物、单元集、工作记录、知识页和索引由新流程重新生成。工程解析 fixture 可复用以做模块测试，但正式端到端验收必须包含从原件转换。

旧数据不参与新链路，也不在本轮计划修订中删除。重新建库的目标位置在实际执行时落实；现在不执行清空或覆盖现有 Vault。

## 2. 删除哪些工作，保留哪些能力

| 项目 | 新决定 |
|---|---|
| 旧 ledger 状态导入 | 删除，不承接 ingested、outputs、revision 等历史记录 |
| 旧 Provider chunk → 新 unit 映射 | 删除，不处理旧文本窗口定位歧义 |
| 旧 Wiki 引用兼容 | 删除，新页面从首次生成就使用 unit_ref |
| 新旧引用双写与 v3 构建记录兼容 | 删除，新构建契约成为唯一入口 |
| legacy_source_id、旧库自动回退 | 删除，新库统一建立来源身份 |
| 旧格式迁移器与迁移 dry-run | 删除 |
| 为旧库维持两条 ingest/query 主路径 | 删除，新系统只维护目标链路 |
| 从新系统回切旧索引 | 删除，开发试验从空库重跑；新系统自身保留一致代次恢复 |
| 新系统之后的重解析、换版与中断恢复 | 保留，这是正常生命周期 |
| 新系统内已生成页面的出处追溯 | 保留，防止未来来源更新后引用失真 |
| Provider 可替换与 coarse-recall 边界 | 保留，这是系统模块边界，不是旧数据兼容 |

因此不再把“旧库能继续运行”列为本项目验收标准。验收变为：**一套明确版本的新代码，从空库和原始文件开始，完整产生可核验的知识与检索结果。**

## 3. 架构调整：来源单元先于 ledger

此前计划让来源单元在 ledger 的自有范围内生成，主要为了接入现有流程。全新设计中应消除这个反向依赖：

```text
bootstrap：契约、配置、注册表、模板、目录
                        ↓
ingest：原件登记 → 解析与规范化产物
                        ↓
来源内容层：section 结构 + owned ranges + source units + 资产关系
              ├─ 工作账本 ledger：任务、领取、进度、QA、覆盖、输出
              │          ↓
              │     reading windows → 候选 → 知识页 → 复核与依赖
              └─ retrieval windows → Provider 索引 → 查询导航
                                                      ↓
                                      读取实际单元并校验来源
```

**内容结构由来源内容层定义；ledger 引用结构与单元并记录工作。Provider 和知识构建都消费相同的来源单元。**

结构生成器可复用现有“父章节扣除子章节范围”的算法，但将其移到共享内容模块。单元生成无需查询 pending/ingested 状态。ledger 不再另存一套权威正文范围，Provider 也不再独立按 Markdown 切出另一套来源身份。

知识构建无需等待向量索引完成。新鲜且获准的来源单元可以被阅读或建立索引，构建产物则在来源依赖及治理校验完成后进入索引。

## 4. 统一对象和职责

| 对象 | 核心职责 | 与其他对象关系 |
|---|---|---|
| Source / Resource | 原件身份、字节指纹、来源归属 | 关联文档、业务版本及接收来源记录 |
| NormalizedArtifact | 一次明确版本的规范文本、outline、资产和解析信息 | 不随检索参数变化 |
| Section | 文档层级、完整 scope、自有范围 | 自有范围由结构算法产生 |
| SourceUnit | 规范来源的连续核心文本或有类型资产单元 | 属于确定来源版本与 section，可精确回读 |
| ReadingWindow | 某次任务要读的单元、上下文与资产集合 | 不拥有新的原文权威 |
| RetrievalWindow | 为 embedding/BM25 构造的索引输入 | 映射 unit_ref 及实际子范围，可重叠 |
| WorkLedger | 工作类型、目标单元、执行状态、覆盖、QA与输出 | 引用 source units，不定义切片 |
| KnowledgeArtifact | 摘要、卡片、概念页、综合页等 | 记录实际支持它的 unit_ref 与页面修订 |

基础来源单元无重叠；阅读和检索窗口允许重叠，按实际核心范围并集计算覆盖。来源单元不等于 Claim，一个事实可以依赖多个单元。

父子关系分开表达：section_parent 管文档结构，context_refs 管阅读上下文，retrieval source_refs 管索引输入映射。不能用一个 parent_id 混合这三种含义。

模型描述、OCR 转录、规范正文和知识页分别带明确类型。表格与图片是来源内容层的一等资产，不因没有向量而无法引用。

## 5. 借鉴 WeKnora 的范围

沿用前轮已核对的设计参考：结构优先与递归回退、父子上下文、内容与附加标题分离、应用层内容记录、统一预览和切分诊断。[WeKnora 策略源码](https://github.com/Tencent/WeKnora/blob/main/internal/infrastructure/chunker/strategy.go)、[Chunk 类型源码](https://github.com/Tencent/WeKnora/blob/main/internal/types/chunk.go)。核对对象为当日官方 main，实施前固定参考 commit。

在本方案中，这些机制由共享来源模块与窗口构造器实现。采用已有 Python 技术栈，不为借鉴算法额外部署 WeKnora Go 服务。不默认接入整个 WeKnora，也不搬用其数据库模型作为唯一真值。

检索粒度和阅读粒度可以不同。结构切分先保留条款、段落、表图关系；模型输入超限由窗口层处理，不因换模型就重定义原始来源。

## 6. 新契约从第一天统一

### 6.1 契约集合

P0 补齐来源单元、工作账本、知识构建、Vault 配置和 Provider 单元能力的最小接口与版本规则。坐标含义、核心/上下文边界、身份归属等先确定；大小和预算是版本化可调参数，不要求在 P0 找到最佳数值。新读取器对不支持的契约明确报错，不静默走旧库路径。

以下为职责概览；P0 已落实的准确契约名、字段和接口原型见共享包规范。生成/读取服务仍待 P2 实现：

- source-units：来源、结构、范围、指纹与资产引用。
- unit-work-ledger：以任务和单元为依据的进度账本。
- knowledge-build：inspections、candidates、outputs、reviews、provenance。
- Vault 配置：本库契约版本、切分规则、构建规则、检索规则。

召回继续遵守 ENVIRONMENT.md 中 `hermes-coarse-recall/v1` 的 Provider 边界与 candidate-navigation-only 权威；同时定义新系统需要的 source-unit capability。Provider 适配器、HTTP/CLI 传输、查询读取器一起实现并校验 source_unit_refs 等扩展。新链路缺少该能力时明确拒绝，不能回到旧行号猜测。若必须破坏协议本体，应先显式修订环境合同，而非隐式更改 v1。

### 6.2 来源身份

新库统一分配 vault_id、document_id、version_id、resource_id，并保留来源机构/接收记录关联。技术版本 ID 必须存在；未知业务版次名称、机构或项目可以明确 unknown/unresolved，不凭空编造。普通来源不应因没有工程版次名称而无法入库。

```text
artifact_revision = H(原件指纹、规范化规则、正文、outline、资产清单)
unit_set_id = H(artifact_revision、来源切分器版本、有效配置)
unit_id = H(来源命名空间、unit_set_id、类型、精确范围、内容指纹)
unit_ref = vault_id + resource_id + artifact_revision + unit_set_id + unit_id
projection_id = H(unit_refs、子范围、标题/上下文渲染、窗口规则)
embedding_key = H(实际索引输入、embedding 模型与预处理指纹)
```

所有 H 使用明确算法和规范序列化。相同输入、来源身份和配置重跑结果一致；完全独立的另一个 Vault 可以有不同 ID，无需让全新建库继承旧库身份。

首版不做模糊跨版本身份继承。新体系发生重解析或修改切分规则时生成新单元集；已有引用继续钉住原单元集，受影响知识页进入复核清单。精细的 moved/split/merged lineage 与自动复用可后置。

### 6.3 单元最小字段

| 字段组 | 内容 |
|---|---|
| 身份 | unit_id、unit_set_id、resource_id、document_id、version_id |
| 来源版本 | artifact_revision、source_sha256、document/asset hashes |
| 定位 | artifact 内相对路径、core span、展示行号、已知页码、定位精度 |
| 结构 | section_id、heading_path、prev/next、asset_refs、context_refs |
| 内容类型 | normalized_source、ocr_transcription、model_derived 等，派生项带 derived_from |
| 可复现性 | schema、splitter_version、effective_config_fingerprint、content_sha256 |
| 质量 | quality_refs、异常及关联审核记录 |

工作状态在 ledger，来源资格在 registry，索引状态在 Provider manifest，不复制为互相冲突的永久布尔值。

新页面的权威引用只有 unit_ref 及其必要子范围。行号、页码、文件链接作为可读展示或定位投影生成，不作为兼容旧引用的第二套权威。

## 7. 切片、存储与读取的具体设计

### 7.1 切片顺序

1. 解析器产出规范正文、结构、资产和 QA 信息，验证来源指纹。
2. 内容模块从 outline 计算 section scope 与 owned ranges；普通 Markdown 经结构解析产生相同接口。
3. 按每个连续 owned range 识别段落、条款、列表、代码、公式与表格关系。
4. 结构内长文本按句子/分隔符递归切分；明确记录无法安全细分的超大结构。
5. 输出无重叠核心单元、精确范围和邻接关系，验证覆盖和资产引用。
6. 发布完整单元集，ledger 和窗口构造器随后消费。

内部文本坐标采用 LF 规范文本上的 Unicode code point、0-based 半开区间；行号展示为 1-based。规范化规则固定版本，不在回读时临时 trim 或重写 Unicode。字符、字节、JS UTF-16 与 PDF 坐标显式转换。

标题面包屑、重复表头等附加内容单独记录，不能计入来源核心范围。表格初期完整资产读取，超大表格仅在结构可靠时按行组构造窗口；表头、单位和脚注关联保留。没有真实 bbox 时不制造图像区域坐标。

### 7.2 存储落点

拟议新库结构，最终在 P0 冻结：

```text
10_Raw/…                                      原件
10_Raw/converted/<resource>/<artifact-rev>/…    版本化规范产物与资产
_system/vault.json                            库身份与契约版本
_system/metadata/source-unit-config.json       来源切分配置
_system/metadata/knowledge-build-config.json   构建默认规则与预算
_system/metadata/retrieval-config.yml          检索控制配置
_system/source-units/<resource>/<unit-set>/    manifest.json + units.jsonl
_system/work-ledgers/…                        单元任务账本
_system/reports/…                             构建、QA、索引审计报告
```

沿用现有 Notes/Cards/Concepts/Projects 的内容用途和原件保护原则，不因为重建自动引入全新知识分类法。

units.jsonl 默认存可验证范围与关系，正文保留在版本化规范产物中，不复制每块小 Markdown。人类阅读使用来源导航页和 get/context 生成的视图。

被新体系页面引用的规范化版本与资产必须保留；Provider 缓存可删除重建。Chroma、BM25、模型和缓存继续在 Provider 主机、Vault 外。main 与 intranet 的既有主机/目录差异继续由部署配置承载。

### 7.3 读取与发布

共享模块提供 build、preview、list、get、context、validate；首版 impact 可以按来源单元集依赖保守生成影响清单。

get 返回 core_text、明确区分的 context、实际 locator、资产、指纹和 QA/治理限制。读取不需要 Provider 在线。

构建先写临时产物，验证后发布完整 manifest/current 指针。锁、expected revision、run journal 和幂等重试保障中断恢复。多文件逐项原子替换不能假装是整批事务；未完成的构建不能获得最终完成状态或提前索引派生页。

## 8. ledger 重新围绕任务设计

不再做“在旧 section ledger 上加字段”的桥接。新工作账本记录：task_id、task_type、task_contract、target unit_refs、运行/领取信息、revision、inspected spans、coverage、QA、输出和失败原因。

推荐首版以一个有界 unit batch 领取任务；章节是分组和阅读上下文。领取检查同一任务范围下的重叠工作，避免两个运行重复提交同一任务；不同任务类型可以消费同一来源单元。

工作项建议状态：pending → running → completed，另有 blocked、failed、skipped。状态精确定义在 P0 固定；失败后通过显式重试恢复，不靠修改来源单元状态。

- knowledge_build 完成表示声明范围内的阅读、候选判断、输出或无输出理由、复核均达标。
- table_qa 的完成不会自动使 knowledge_build 完成。
- 被当作上下文展示的单元不自动算作已处理。
- 没有可提炼知识可以合法完成，但需要完整覆盖记录与无输出理由。
- QA 限制单独传播；任务终态不意味着证据可信或业务版本获准发布。

章节进度、来源导航页等从单元归属与工作记录生成，不成为另一套状态真值。多来源页面的支持关系由统一 provenance/依赖记录支撑，所有相关账本视图都应能查到该输出；治理校验直接核对完整依赖，不依赖复制到某一个章节的偶然记录。

## 9. bootstrap 和 ingest 从头贯通

### 9.1 bootstrap 提前成为开发基础

bootstrap 创建一个明确版本的空库，包含来源/治理注册表、来源单元配置、账本目录、构建规则、引用模板、查询规则、验证入口和环境报告。

来源单元层是新库标准能力，不是可选试验开关。profile 可以决定业务模板和治理细节，但不能绕过统一来源身份与引用契约。只对本轮明确支持的格式承诺完整 ingest 能力。

bootstrap 不解析文件、不生成假来源、不建空的“ready”索引，也不把模板知识当作已经核验的来源。后续能力默认配置必须能被 ingest 和 query 实际消费，而非只生成没人读取的配置文件。

### 9.2 新 ingest 主流程

1. 登记并保存原件，确定来源身份与已知业务归属。
2. 解析并生成新版本规范产物，完成结构/资产校验。
3. 生成并发布 source units。
4. 根据摄取范围建立工作任务，组合 reading windows。
5. 阅读核心文本和必要图表，记录覆盖、QA 与待补读范围。
6. 形成候选；按项目、机构、对象和版本区分同名实体。
7. 必要时补读条件、定义、例外条款，汇集跨来源证据。
8. 写知识页，登记实际支持候选/页面段落的 unit_refs。
9. 复核正文、来源归属、适用范围与 QA，提交来源依赖和工作结果。
10. 运行来源、构建、治理、lint 校验；需要 QA 的结果明确保留限制。
11. 按配置及资格从来源单元和获准知识页生成检索投影，显式同步 Provider。

source preparation 和 knowledge construction 的分阶段能力可以保留，但前者完成不冒充整项知识构建完成。第一次实践必须跑完主流程，不能在“已经生成 chunks”处停止。

### 9.3 阅读材料与 Provider 输入

Wiki 阅读材料是任务材料包：核心单元、补充上下文、图表、来源身份、适用范围、质量限制及未读清单。它由构建任务组织，不以向量召回 Top K 为唯一范围。

Provider 只负责生成检索窗口及索引，窗口保存 unit_refs/subspans。新查询收到命中后核对投影与来源版本、当前治理资格、核心原文和证据，不能将 snippet 当最终来源。

派生知识页可进入检索，但必须标记派生类型、页面修订与支持来源。来源层查询默认不混入自己的派生摘要反复充当原始证据；需要通过知识页扩展线索时仍回到原始 unit_refs，避免循环自证。

## 10. 首版范围与后置项

首版必须完成：工程 PDF 原件到规范产物；结构/条款/公式及表图资产的单元化；来源身份；精确回读；工作账本；跨来源知识构建；Provider/query；bootstrap/lint；恢复与重跑。增加一种普通 Markdown 来源用于验证内容接口没有绑死 PDF。

Word/其他 Office、独立图片先以统一 adapter 接口设计。若它们进入正式重摄取清单，必须在最终 bootstrap 前完成相应原件到单元的集成验收；不能把“格式统一后置”误解为允许实际材料被悄悄跳过。确切格式清单在 P0 盘点，额外格式复杂度单独估算。

可以后置：跨版本精细 lineage、自动最小范围重写知识页、逐句 Claim 图谱、复杂 Excel 单元格语义、全量图像自动描述、可视化单元编辑器、分布式调度和数据库后端迁移。

新体系的后续版本处理先采取简单可靠的方式：保留被引用版本、产生新单元集、识别依赖页面、显式复核/重建。无需为第一版开发智能跨版本映射。

## 11. 重排后的实施计划

以下为一名熟悉仓库的开发者的粗略净开发估算，含常规测试，不含大批量解析等待、业务专家评审或新增复杂格式。首版核心约 18–28 个工作日；取消兼容减少工作，但新账本和 bootstrap 贯通仍需真实实现，不能承诺机械压缩一半工期。

| 阶段 | 主要工作 | 验收交付 | 估算 |
|---|---|---|---|
| P0 最小接口与规则 | 沿用已确定决策，补齐共享类型、必填规则、接口、配置及正反样例 | 可供后续模块共同实现的最小契约，不重复写宏观方案 | 2–3 日 |
| P1 新 bootstrap | 更新脚本、模板、规则、配置与 lint；建立共享模块打包入口 | 从空目录创建目标库，结构与契约校验通过 | 2–3 日 |
| P2 来源内容层 | 规范产物 adapter、section ownership、结构切片、资产关联、get/context/validate | 一份原件到 source units 全流程；精确回读、覆盖与确定性通过 | 4–6 日 |
| P3 账本与知识构建 | 新任务状态机、阅读窗口、候选/页面 unit_ref、复核和依赖提交 | 多来源真实页面可回查，零候选与中断恢复可用 | 3–4 日 |
| P4 Provider/query | 单元投影、窗口预算、索引 manifest、传输能力、来源回读、派生页资格 | 端到端查询与 QA/治理校验通过；Provider 可重建 | 3–5 日 |
| P5 完整实践 | 每轮新建测试库，从原件重新 ingest；质量评测、格式补齐与流程修正 | 无手工补账本/补引用的完整演练报告 | 2–4 日 |
| P6 发布与正式重建 | 完成 main/intranet 验证发布；部署一致代码；新正式库 bootstrap + ingest + query 验收 | 新库完整构建报告、索引状态、引用与问题清单 | 2–3 日 |

bootstrap 在 P1 是可运行基础，后续阶段同步补齐实际配置和验证器；到 P5 必须从空目录重跑以证明早期试验文件没有掩盖初始化缺口。P6 的正式 bootstrap 是最终重建，不依赖开发试验库的状态。

共享切片/读取逻辑只维护一份源码。按实际部署方式，必需模块和资源随 Skill 目录交付，脚本按自身位置加载；Provider 所需部分随 Provider 发行。允许由源码生成内置副本并核对指纹，禁止人工分别维护。用户不需要额外安装共享 wheel、设置 PYTHONPATH 或保留仓库根目录。

### 11.1 P0 是什么：补齐程序之间的约定

这里 P0 表示阶段 0，不是缺陷优先级，也不是每次 ingest 都要执行的步骤。它是本次开发开始时，把已有方案中尚属自然语言的关键规则落实为机器可读定义和小样例。完成一次后由代码共享，用户不需要在每次 bootstrap 时重新设计。

P0 不要求重新发明文档身份、QA、治理或知识构建方法，也不要求先写出全套切片算法。下表区分已有基础与新增工作：

| 主题 | 当前已有 | P0 还需补齐 |
|---|---|---|
| 原件与规范产物 | Bundle v2 的 manifest、document、outline、资产、指纹及校验器 | 新 artifact_revision 与 unit_set 的引用规则，明确哪些现有字段直接复用 |
| 文档治理 | document/version/resource 身份与 registry | 新 unit_ref 必须如何关联这些身份；无需另造一套业务身份 |
| 章节范围 | ledger schema 1.0、content_ranges、revision 与状态机 | 将内容归属定义移到来源层；规定新任务按什么单元领取和计覆盖 |
| 知识构建 | 当前代码 hermes-knowledge-build/v3，候选、阅读证据、页面复核 | 单元引用、实际子范围和完整依赖如何进入新的唯一记录 |
| Provider | hermes-coarse-recall/v1，路径/行号/指纹/provider_ref | 新 source-unit capability、窗口到单元映射与缺字段的明确错误 |
| 新架构方向 | 本计划已有 source_unit/reading_window/retrieval_window 等定义 | 程序类型、必填/可空字段、枚举、函数输入输出及可执行样例尚未实现 |

依据：[Bundle 契约](../hermes-obsidian-controlled-ingest/references/mineru-pdf-bundle.md)、[Bundle 校验器](../hermes-obsidian-controlled-ingest/scripts/validate_document_bundle.py)、[当前 ledger](../hermes-obsidian-controlled-ingest/scripts/manage_bundle_ingest.py)、[构建校验器](../hermes-obsidian-controlled-ingest/scripts/validate_knowledge_build.py)、[Provider 契约](../qmd-like-rag/src/qmd_like_rag/contract.py)。文档与源码的版本差异需以当前代码核对，不能因为某份 ADR 仍写 v2 就把实际 v3 当不存在。

本轮 P0 已交付新的 source-unit schema、共享读取接口原型、离线精确引用校验和正反样例；实际生成器与原文读取器仍待 P2。列出字段名称不等于已经实现运行服务，验收记录明确区分这两者。

例如“start/end 是位置”仍不够：P0 要明确它是哪个版本文本上的 code point、0-based、半开区间，标题上下文是否包含在内；P2 才实现计算和校验。P0 规定“超大表格保留完整来源并报告 oversized”，P4 再实现怎样把它转成不超模型预算的检索输入。

### 11.2 P0 的最小交付与结束条件

| 子任务 | 具体交付 | 完成判据 |
|---|---|---|
| P0.1 复用清单与少量决策 | 简短 ADR：来源层权威、ledger 消费关系、单元/窗口区别、原件保护、首版格式和共享包位置 | 已定方向引用本计划；只处理真正未决项，不重做长篇调研 |
| P0.2 共享类型与校验约束 | 来源单元、unit_ref、窗口 source_refs、工作记录和知识引用的 schema/类型草案；参数分为来源切分、阅读预算、检索预算 | 类型及必填约束可供程序读取；约定范围/指纹/跨来源一致性需由业务校验补充，不能假装 JSON Schema 能全包 |
| P0.3 接口和配置边界 | build/preview/get/context 的输入输出；数据/算法版本规则；source-unit capability；默认配置样例 | ingest 与 Provider 依赖同一共享接口；明确未知格式、损坏引用、超限与 QA 的返回语义 |
| P0.4 小型正反样例 | 一小段带标题、条款、表格链接的规范文本及期望单元、窗口、引用；缺字段/非法范围/混版本反例 | 样例能通过基础 schema 检查，非法样例被拒绝；回读/哈希与生成一致性留到 P2 实测 |

交付可以集中为一个共享契约模块、配套 schema/examples 和一份短 ADR，不需要创建四套重复文档。后续模块实现时补充细节，禁止无限延长 P0 去预设计所有未来功能。

本节 P0.1–P0.4 已由独立共享包、ADR 和样例测试落实，详见 P0 验收记录。可以开始 P1/P2；完成切片质量评测、调出最终窗口大小不是 P0 的退出条件。

### 11.3 WeKnora 如何承担这些职责

没有证据表明 WeKnora 把其开发过程命名为 P0；这里对比的是程序中的同类职责。

| 职责 | WeKnora 的对应落点 | 我们的对应安排 |
|---|---|---|
| 对外切分配置 | KnowledgeBase 的 ChunkingConfig，定义大小、重叠、分隔符、父子参数、策略等 | P0 区分来源切分配置与消费窗口配置，P1 写默认值，P2/P4 实际消费 |
| 切分函数输入输出 | chunker 的 SplitterConfig 与 Chunk；正文、ContextHeader、Seq、Start/End 分开 | P0 共享类型和坐标规则，P2 实现生成器 |
| 应用内容记录 | types.Chunk 包含材料归属、内容、位置、父/邻接关系、类型等 | P0 定义 SourceUnit 持久化记录，P2 存为可验证来源切片 |
| 有效参数与父子规则 | NormalizeSplitterConfig、DeriveParentChildConfigs、SplitParentChild 等共享函数 | P2 来源路由/上下文构造，P4 检索预算转换；预览和正式生成共用规则 |
| 结果检查 | ValidateChunks 检查空结果、长文未切开、碎块过多及块大小异常，策略入口可以回退 | P2 增加本方案要求的精确回读、无重叠覆盖、指纹/资产校验；无效结果不发布为可用单元集 |

依据：[配置类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/knowledgebase.go)、[切分类型](https://github.com/Tencent/WeKnora/blob/main/internal/infrastructure/chunker/splitter.go)、[应用 Chunk 类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/chunk.go)、[共享策略入口](https://github.com/Tencent/WeKnora/blob/main/internal/infrastructure/chunker/strategy.go)、[结果校验](https://github.com/Tencent/WeKnora/blob/main/internal/infrastructure/chunker/validator.go)。

WeKnora 的这些约定已经进入 Go 结构和运行代码，并不是要求每个用户先写一份 schema 再上传文件。我们应达到同样的效果：开发时定义好，新库使用默认配置即可开始，只有实际业务偏好需要配置。

不能据此声称它提供了我们这里全部严格保证：结构校验不等于内容逐字一致或全部覆盖；所查策略入口末级仍可能返回 legacy 结果。我们的“零核心重叠、完整回读、不可变来源版本”是本方案额外明确的约束，不是对 WeKnora 全路径现状的描述。

### 11.4 P1：bootstrap 细分

| 子任务 | 工作 | 可验收结果 |
|---|---|---|
| P1.1 库骨架与身份 | 创建新库 ID、注册表、来源/账本目录，声明支持的契约 | 空库身份和目录可被验证 |
| P1.2 实际配置 | 写来源切分、阅读/构建和检索预算；解析配置优先级并记录有效配置 | 程序可加载配置；无模型/主机依赖的值保持可移植 |
| P1.3 模板与流程入口 | 更新知识引用模板、ingest/query 指令、lint 接口；将所需共享模块和 schema/defaults 内置到 Skill | 通过脚本自身路径加载；不要求额外安装共享包或假定 jsonschema 已存在 |
| P1.4 空库与复制部署验收 | 将完整 Skill 复制到独立目录，在无仓库源码路径、无 PYTHONPATH/外部包可见性的环境中运行；校验重复运行及非空目标处理 | 保持用户现有复制部署方式；无需手工补配置或 pip install；bootstrap 不冒充内容摄取完成 |

### 11.5 P2：切片、父子上下文、存储读取细分

P2 是切片系统的主体，不是只交 schema 或往旧 Provider 块增加字段。以下步骤按依赖实现，验收不要求 Chroma 在线。

| 子任务 | 输入与工作 | 输出及完成判据 |
|---|---|---|
| P2.1 来源适配与坐标 | 原件经过解析生成规范产物；统一 LF、身份/指纹和行字符映射 | artifact 读取接口；中文/emoji/CRLF 坐标样例正确 |
| P2.2 章节结构与自有范围 | Bundle 优先用可靠 outline；普通 Markdown 解析结构；父 scope 扣子 scope | Section + owned ranges；无双重归属，不依赖 ledger 状态 |
| P2.3 受保护结构 | 识别代码围栏、公式、条款、列表、表格与资产边界 | 结构块清单及异常；表题/单位/脚注关联不丢失 |
| P2.4 结构优先切分 | 短结构在同一归属内合并，长章节内部细分，不跨不连续 owned range | 核心无重叠 SourceUnits；每块能精确回指原文 |
| P2.5 递归与有限启发式 | 按段/句/分隔符递归；缺可靠标题时识别编号/分隔线；明确 auto 路由 | 选中策略、回退和 oversized 原因可查；不使用 LLM 决定基础边界 |
| P2.6 章节/相邻与父上下文 | 生成所属 section、prev/next；实现预算内组合单元的 context 接口 | 小单元可扩展到有界父上下文；核心和附带内容区分，跨子章节有明确引用 |
| P2.7 资产与精确引用 | 文本、表格、图片有类型 locator；正文和上下文哈希边界明确 | unit_ref 可解析；图表引用完整，未知页/区域不伪造 |
| P2.8 存储与发布 | manifest + units.jsonl；版本化规范产物；完整验证后发布指针 | ID 确定性、重复文本不碰撞；失败构建不能被当作完整来源集 |
| P2.9 读取与预览 | build/preview/list/get/context/validate 接入同一实现 | 可查看边界和诊断、按 ID 回读原文；preview 与 build 对同输入一致 |
| P2.10 独立验收 | 从一份真实原件生成、读取、扩上下文并核对覆盖；测试损坏/不完整产物 | 无 Provider、无 Wiki 的共享切片系统能独立完成上述行为 |

WeKnora 的 heading/recursive/heuristic/auto 能力对应 P2.2–P2.5，但我们优先使用已有可靠 outline；没有必要先做复杂文档画像评分。P2.6 实现父上下文的通用组合能力，P3/P4 决定任务预算与何时扩展。

基础 SourceUnit 尽量直接作为检索输入的核心。大小不合适时 P4 才合并相邻单元或细分到子范围，并记录精确映射。不同名称不意味着必须把同一份文本实体存三份。

### 11.6 P3：账本与 Wiki 复用细分

| 子任务 | 工作 | 可验收结果 |
|---|---|---|
| P3.1 单元任务与领取 | task type、目标 refs、revision、重叠任务检查、中断重试 | 同一任务不重复提交，不同任务可消费同一单元 |
| P3.2 阅读材料包 | 从 source units 构造 reading window，记录核心、上下文、资产和预算截断 | 展示上下文不冒充实际覆盖；遗漏范围可列出 |
| P3.3 候选与补读 | 记录候选及支持 refs，按对象/适用范围补条件和例外 | 同名对象不混并；有候选/无候选都能交代读取与判断 |
| P3.4 页面引用与复核 | 页面段落/候选对单元的支持关系、output review、完整来源同步 | 每个实际来源可回查；派生页不循环充当原始来源 |
| P3.5 依赖与恢复 | 提交账本/页面/依赖的运行记录；失败可重试；新版本影响报告 | 没有隐式双写旧引用；任务完成、QA和可见性分别判定 |
| P3.6 跨来源实践 | 使用 P2 输出完成一次真实知识构建 | 多来源页面引用准确；不依赖先建向量索引 |

### 11.7 P4：Provider 与查询细分

| 子任务 | 工作 | 可验收结果 |
|---|---|---|
| P4.1 单元语料入口 | 按来源资格读单元及获准派生页，替换 Provider 独立来源切块入口 | 每个新检索输入都有真实来源 refs，不能退回粗行号猜测 |
| P4.2 检索窗口 | 默认直接消费单元；必要时合并、子范围细分、有限 overlap；附带标题单列 | 总输入按模型 tokenizer 计预算，窗口 → 单元/子范围映射可验证 |
| P4.3 存储与指纹 | Chroma/BM25 同投影 manifest；窗口、模型、来源变化分别失效 | 改模型不改来源 ID；重建不产生混代结果 |
| P4.4 传输与能力校验 | CLI/HTTP/normalizer 一起传 unit_refs、projection 指纹及定位精度 | 字段不在适配中丢失，缺能力明确报错 |
| P4.5 命中与父上下文 | 命中窗口 → 真正来源单元 → 有预算的相邻/章节上下文；治理再检查 | 小范围匹配、大范围理解；上下文去重，不跨资格边界泄漏 |
| P4.6 端到端验证 | 原文引用核验、索引不可用、过期投影、无答案、派生页依赖场景 | 查询只读，回答回到原文，而非把 snippet 当最终证据 |

### 11.8 P5/P6：完整实践与正式重建细分

| 子任务 | 工作 | 可验收结果 |
|---|---|---|
| P5.1 全新实践库 | 用统一代码 bootstrap，从至少两份相关原件开始 ingest | 解析、切片、账本、知识页、检索与查询贯通 |
| P5.2 质量评测 | 标注问题与支持范围，检查条款/例外/表格和同名对象；比较新窗口策略 | 输出定位正确性、证据覆盖、知识质量与检索性能报告 |
| P5.3 故障和更新演练 | 各阶段中断、重复输入、改解析/窗口/模型、来源失效 | 恢复与失效规则符合第 12/13 节 |
| P5.4 第二次空库重跑 | 修正代码后再次从空库与原件构建，覆盖正式清单的格式 | 无手工补账本、引用、配置，流程可重复 |
| P6.1 一致发布 | main 验证推送，merge intranet 保留部署值；部署核验 | Skill、共享模块、Provider、配置及验证器版本匹配 |
| P6.2 正式 bootstrap | 在明确的新目标库初始化，核对配置、格式能力和原件清单 | 新正式库可投入摄取，不依赖试验库状态 |
| P6.3 正式 ingest 与验收 | 全批新解析/单元/知识构建/索引/query；记录失败或 QA 待办 | 每份材料有可核对结果，无静默遗漏或虚假完成 |

阶段编号表达依赖和验收里程碑，不要求每项单独开 PR。P0 完成最小定义后即进入实现；P1/P2 初期即可有第一个可运行检查点。细分没有增加旧数据兼容工作，原工期仍是粗估，不能把子任务行数当作工期天数。

### 11.9 主要代码落点

| 现有组件 | 改动方向 |
|---|---|
| bootstrap 脚本/模板/配置 | 新库契约、单元/账本目录、默认规则及验证入口 |
| MinerU/规范化转换脚本 | 输出共享来源 adapter 所需身份、结构、资产和指纹 |
| `manage_bundle_ingest.py` | 内容范围算法抽到共享层；工作状态按新账本重新实现 |
| `validate_knowledge_build.py` | 单一新构建记录；验证 unit_ref、覆盖、review 与依赖 |
| `sync_knowledge_provenance.py` | 从真实单元引用生成页面来源与完整依赖记录 |
| qmd-like-rag chunker/indexer/stores | 去掉独立来源身份生成；消费单元并生成检索投影 |
| query 适配与来源定位脚本 | 保留 unit_refs、精确读取、投影校验和治理检查 |
| vault lint / Skill / prompt 文档 | 一起切到新流程，不留相互矛盾的执行说明 |
| 测试 | 新契约 fixtures 替换旧格式断言；保留原件保护、治理、只读与部署边界测试 |

旧数据格式不再作为新代码验收要求，但不能以删除旧测试为由失去它们覆盖的有效业务约束。代码入口可重命名或重新组织，由目标职责决定，不为保持旧调用习惯而保留冗余层。

## 12. 实践与验收

### 12.1 第一轮纵向实践

用新 bootstrap 建一个空测试库，输入一份工程手册原件和一份相关规范原件，跑通：解析 → 单元 → 任务 → 跨来源知识页 → 复核 → 索引 → query → 原文回读。

要求至少覆盖一个完整条款、一张表格、一个公式/图片证据场景，以及一个需要条件/例外补读的问题。不得从旧库复制章节完成记录、已生成页面或来源同步结果。

### 12.2 后续实践矩阵

| 场景 | 验收要求 |
|---|---|
| 空库 bootstrap | 仅靠新代码和配置可重现全部必要结构 |
| 原文覆盖 | eligible 内容核心范围全覆盖且无重叠；排除部分有类型与理由 |
| 精确引用 | 单元回读哈希、字符、行与资产定位一致 |
| 重复文本与编码 | 重复段落 ID 不碰撞；中文、emoji、CRLF 等定位正确 |
| 任务覆盖 | 上下文展示不冒充完成；无候选有完整阅读与判断记录 |
| 跨来源写页 | 所有实际支持来源登记完整，条件和适用范围可查 |
| QA 与治理 | 任务完成不等于发布；受限来源不能通过窗口/派生页泄漏 |
| 中断恢复 | 解析、单元发布、页面提交、索引中断各能恢复；不能虚假 ready |
| 重复 ingest | 同一库同一原件和配置可识别已处理输入，不重复建对象或页面 |
| 新体系内更新 | 重解析产生新版本；已有引用保持原义，影响页被发现 |
| 改 embedding | 只重建相应投影/向量，不更改来源单元身份 |
| 查询只读 | query/get/context 不隐式构建或修复内容与索引 |
| 无 Provider | 来源回读、QA 和知识构建仍可运行 |
| 最终从零重跑 | 新空库再次 ingest 全批材料，无需手动补写内部文件 |

建立 30–50 条人工标注查询，覆盖条款、例外、表格参数、同名对象、多来源比较和无答案。比较不同新窗口策略时固定模型和语料，不依赖旧索引。可用本次新解析材料重建简单基线算法做性能对照，但不为此引入旧数据适配代码。

评估来源正确率、关键证据覆盖、知识页限定条件、Recall@K、上下文 token、P50/P95 延迟、构建耗时、索引大小和重复阅读。来源错配/治理泄漏必须为 0；其余目标在 P0 预先设定。本文未执行这些评测。

## 13. 发布与运行后的版本管理

开发阶段允许在新测试目录丢弃试验产物后重新 bootstrap；正式新库发布后，恢复使用该新体系自身的 run journal、manifest 和完整索引代次，不切回旧数据模式。

取消旧内容迁移不等于取消版本管理。未来原件业务版次、解析器、切片规则和模型仍会变化：

| 变化 | 新体系处理 |
|---|---|
| 原件或业务版次变更 | 新资源/版本，按已知业务关系登记 |
| 同原件重解析 | 新 artifact_revision，保留被引用产物 |
| 来源切分规则变更 | 新 unit_set；依赖页面进入复核清单 |
| 检索窗口变更 | 重建投影，不修改来源单元 |
| embedding 变更 | 重建向量，保留来源与知识引用 |
| 来源撤销/访问变化 | 查询立即检查当前资格，写侧清除失效索引 |

共享修改仍按 [分支维护合同](../BRANCH_MAINTENANCE.md) 先 main 验证提交推送，再在干净工作树 merge 到 intranet，保留部署差异；部署时核对实际 WSL 环境。Skill scripts 的入口保留 shebang 和 Git 100755 模式。

最终交付是：新契约与代码、完整实践证据、可重复执行的 bootstrap/ingest 流程，以及实际新库的构建和查询验收。无需交付旧库迁移工具或旧内容兼容层。
