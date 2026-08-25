# Controlled Query 时间优化设计

## 文档定位

本文件是 `hermes-obsidian-controlled-query` 的维护者参考，记录真实性能事故、可控调用边界、实现策略和回归验收。保留在该 Skill 的 `references/` 下是合适的：内容直接约束 `query_session.py`、trace 和 evidence decision，不属于 Vault 业务知识，也不应在普通查询中默认加载。只有性能优化、trace 诊断或 Skill 维护时才读取。

若将来出现跨多个 Skill 共用的 Hermes API/VPN、模型服务或全局 prompt-compaction 运维规范，应另建仓库级运行手册，并从这里链接；不要把本文件复制到 Vault 或 Provider 运维文档中造成双份事实来源。

## 背景与目标

一次典型的单问题证据查询耗时约 6 分 45 秒。原 trace 表明候选检索本身只占约 1.4 秒；主要时间消耗来自模型与工具之间的多轮编排、反复读取和格式化、逐项证据处理，以及最后逐条写 trace。因此优化顺序是：

1. 先减少模型—工具往返和返回上下文；
2. 再压缩文件读取和 trace 写入次数；
3. 最后补齐后半段阶段记录与请求级计时，避免可观测性反过来增加查询步骤。

普通单问题查询的结构目标是两次 query-session 调用：一次组合 `query` 和一次 `finalize`。只有显式的用户/审计视觉核验要求才增加一次确定性 `verify` 准备和一次视觉核验。工程参数、公式、表格、图片和 Bundle QA flag 本身不触发该路径；普通查询信任 Bundle，并在有具体 QA 问题时如实降级或限定结论。每个用户请求另有一次 `bootstrap`，多题最后一次 finalize 通过 `--close-request` 同时返回汇总。端到端验收以 intranet 上等价 A/B 为准，暂定普通证据查询 P50 不超过 180 秒，并以接近或低于 120 秒为进一步目标。脚本耗时、模型思考时间、网络/API 重试、审批等待和回答输出时间必须分开报告。

## 2026-08-18 真实双题事故

Hermes session `20260818_162026_79f45a` 使用 GPT-5.5 回答两个问题，实际用户端到端约 349.2 秒；controlled request 只记录 262.64 秒。17 次模型调用全部成功，没有 API 自动重试，模型 API 总等待约 292 秒。两次 RAG/层级并行检索工具执行约 47 秒，因此本轮高耗时的主因是调用轮数和大上下文，不是 VPN 失败重试。

### PDF 核验试错链

该次问题涉及多个工程参数，`inspect` 返回了转换章节和原 PDF 页码，但没有可直接查看的 evidence image 或 viewer URL；Hermes 运行环境也没有预先声明可用的视觉渲染能力。Skill 只规定“核验原页”，没有规定能力不可用时的单次停止条件。模型因此自主尝试：

```text
pdftotext
-> 检查 Python PDF 库
-> 检查其他 PDF 命令
-> 查页面文件
-> 枚举 Bundle
-> 搜索转换文本
```

其中 `pdftotext` 本身是文本抽取工具，并不能完成要求的视觉原页检查。其余路径也没有产生可核验页面。六轮模型—工具往返约消耗 101 秒，最终仍把工程参数错误标成 `clear`。

这条试错链的成因不是单一依赖缺失，而是三个契约缺口叠加：

1. `inspect` 没有明确返回 verification readiness；
2. 运行时没有一个受支持的、确定性的 page carrier 准备接口；
3. 核验不可用时没有强制停止、证据降级和 unresolved 门禁。

同时，模型提交了 `unresolved_items`，旧脚本只读取 `unresolved` 且静默忽略未知字段，导致 trace 显示 `None recorded`。第二题又在第一题 finalize 之前开始，两条 trace 重叠约 80.43 秒，使第一题的 `answer-synthesis` 阶段被第二题工作污染。

### 已采用的解决策略（P1-P6）

1. **P1 · 确定性核验**：新增显式、领域无关的 `--verification-required` 策略和 `verify`。`query_session.py` 不读取问题关键词、设备名、参数名或语言来猜测是否核验；调用方根据证据要求显式选择。`verify` 只使用 inspect 注册的 evidence image、viewer URL，或单一受支持的 `pdftoppm` 页渲染；结果只可能是 `ready`、`unavailable` 或 `failed`。后两者立即要求 `needs-qa` 和 unresolved，禁止继续探测 `pdftotext`、Python PDF 库、其他二进制、Bundle 文件或转换文本。
2. **P2 · 请求 bootstrap**：一次返回实际规则文件内容、部署配置、session linkage 和 verification runtime，替代配置搜索和分轮读取。
3. **P3 · 自动收口**：最后一题 `finalize --close-request` 同时校验请求完整性并返回 request capsules，取消独立 request-summary 模型轮次；独立 `request-summary` 只保留给稍后检查或调试。
4. **P4 · 顺序门禁**：同一 request 存在 `in_progress` trace 时拒绝新 begin；校验 `--question-count`、连续 index 和重复 index；summary 记录并检查 trace overlap。
5. **P5 · 分层 decision schema**：顶层 decision 和 claim 字段保持严格，`unresolved_items` 作为兼容别名；event 标准字段固定，模型新增的 event 字段保留到 `extensions`，但不能满足任何门禁；只有显式要求视觉核验时，未核验 evidence 才不得使用 `clear`/`source-backed`；verified ref 必须对应带实际 carrier 路径的完成事件。
6. **P6 · 上下文压缩**：inspect 返回紧凑 QA/verification packet，完整 provenance 留在 sidecar；answer capsule 使用去重 `sources` 和 claim `source_ids`；最后收口不重新加载 evidence packet。

显式视觉核验路径为：

```text
bootstrap（每请求一次）
-> query（创建 trace 并自动 inspect 首个有界窗口）
-> verify（一次；不可用即停止）
-> visual check（仅 ready）
-> finalize [--close-request]
```

## 2026-08-19 三题回归暴露的编排浪费

三题 controlled request 约 290 秒，Hermes 用户端到端约 350 秒；23 次模型调用累计 API 等待约 246 秒，未发生 API retry。前两题分别约 70 秒和 31 秒，第三题约 175 秒并出现两次 supplement。主要浪费不是检索计算或 VPN 重试，而是四次可避免的失败与随后重新加载/重试：

1. finalize event 使用模型生成的 `type` 字段，被严格 schema 拒绝；
2. 已知 `document::section` 不在 fused top-k 时，inspect 拒绝精确章节；
3. 上一次 supplement 尚未 inspect 就发起下一次 supplement；
4. 精确章节选择再次失败后退回临时文件搜索，证据没有完整进入 Claim–Evidence map。

当时的通用修复不根据题目关键词或章节编号做特化：inspect 可从 query projection 解析任何已注册的精确 `document::section`，同时仍拒绝任意路径/行号；inspect 返回紧凑 finalize contract；未知 event 字段进入 `extensions`，而顶层 decision、claim、evidence ref 和 verification gate 继续严格。该 projection 外选能力已被后文“回归稳定版与单轮硬门禁”取代，当前只允许 `begin` 实际返回窗口。

## 2026-08-19 内网单题近 20 分钟中断事故

内网使用一个包含多个工程参数的单题执行回归，trace `20260819_065942_2e9da9ca` 从 `06:59:42Z` 记录到 `07:17:30Z`，墙钟跨度 1068 秒（17 分 48 秒），随后在尚未 finalize 时中断。trace 保持 `in_progress`，没有 accepted evidence、Claim–Evidence map 或 conclusion。

已记录的 primary stage duration 为 958127.346 ms，其中 `candidate-review` 和四次 `evidence-gap-review` 合计 957557.064 ms，占已记录耗时 99.94%。检索、文档读取、表格解析和 provenance 脚本均为毫秒级。剩余约 109.9 秒位于未计时的模型/外部工具间隔。由于该部署没有传入 Hermes session/message linkage，只能确认时间消耗发生在 query-session 调用之间，不能仅凭 trace 继续拆分模型 API 排队、生成、shell 工具和网络等待。

### 触发链

1. intranet 的 qmd-like-rag 部署开关为 disabled，查询按设计继续走 hierarchical fallback；开关本身没有产生等待，但失去了可与层级路线互补的粗召回。
2. hierarchical locator 先按文档选取范围，再把所有章节放入一个全局 section 排序。同一份来源因重复出现主题词而占满候选窗口，另一份已注册在 query projection 中的相关来源没有进入 compact top-k。supplement 使用相同排序后又返回近似候选。
3. 模型通过额外搜索找到了工作手册的精确章节，但 query projection/manifest 首先提供了 Vault 外仍存在的原 ingest 路径 `/opt/data/phq/2026.6.12/...`。`resolve_source_path()` 接受该文件，`build_evidence_packet()` 随后调用严格的 `vault_relative()`，对 Vault 外路径抛出异常。Vault 内实际已有位于 `10_Raw` 子目录中的原 PDF 副本，但旧 resolver 只尝试 `10_Raw/<filename>`，不会递归定位。
4. 模型在正常 query 中读取并多次 patch 已部署 Skill，先尝试接受外部绝对路径，再尝试优先内部副本，最后尝试放宽 finalize 的 `validate_vault_path()`。这违反只读查询和 Skill 维护边界；放宽绝对路径校验还会破坏 Vault evidence containment。
5. 第一次成功登记的 evidence handle 已保存外部路径。后来同一章节重新 inspect 时，`register_evidence_packets()` 只追加 inspection round，不刷新 provenance，因此 finalize 继续读取旧路径并失败。
6. Skill 文本要求一次 inspect、真实缺口时最多再 inspect 一次，但脚本没有硬限制。本轮实际执行五次 inspection；模型返回中出现剩余 iteration 倒计时，最终在修补/重试链中耗尽执行轮次。

### 责任边界

- **确定性代码缺陷**：Vault 内原件解析顺序及嵌套目录支持、重复 inspection 的 evidence catalog 刷新、全局 section 排名缺乏跨文档多样性、inspection/supplement 只有文字约束而没有硬门禁、失败事件未自动进入 trace。
- **模型编排错误**：偏离 `begin -> inspect -> finalize`，在 query 中修改已安装 Skill，提出接受 Vault 外绝对 evidence 路径的错误修法，在已有两轮 inspection 后继续搜索和重试，没有用 `incomplete + unresolved` 及时收口。
- **部署/运行时条件**：intranet Provider 明确关闭；Hermes session/message 环境未传给 query-session。前者放大 fallback 候选质量问题，后者削弱耗时归因，但两者都不是路径异常本身。

### 采用的通用修复

1. **Vault 内原件优先**：只把 Vault 内文件登记为 `original_asset_path`。先检查 manifest/query projection 中可解析为 Vault 内的候选，再检查 `10_Raw/<filename>`；必要时才在 `10_Raw` 下按精确文件名递归定位。manifest 有 SHA-256 时必须匹配；多个同名匹配无法唯一确定时返回 unresolved。Vault 外 ingest 路径只保留为 control-plane 诊断元数据，不进入 evidence catalog，`validate_vault_path()` 继续严格拒绝绝对路径和 traversal。
2. **可恢复的 evidence handle**：同文档版本、同 section 再次 inspection 时复用 handle，但刷新页码、block、原件路径、viewer、QA 和 verification assets；若 document version 发生变化则拒绝静默覆盖并要求新 trace。
3. **跨文档候选覆盖**：hierarchical locator 在最终 section 窗口中先保留最多三个不同文档的最佳章节，再按原始分数补齐剩余位置，并返回 ranking strategy/document count。该策略不依赖任何具体领域词，也不改变 evidence authority。
4. **硬性止损**：一个 trace 最多两次 inspection、一次 supplement。超限调用返回结构化 `blocked -> finalize`，证据不足时明确要求 `status: incomplete`。query-session 未预期异常自动记录 `query-command-failure` 和 `recommended_next_command: finalize`；模型不得在查询中 patch Skill 或放宽路径边界。
5. **回归覆盖**：增加“外部路径存在 + Vault 内嵌套副本 + 同名错误副本 + SHA-256 选择”“无 Vault 内副本时不提升外部路径”“重复 inspection 刷新 catalog”“第三次 inspection/第二次 supplement 被阻止”“失败事件落 trace”“单文档高重复分数不能占满 compact window”等测试。

这些修复属于 main/intranet 共用的领域无关查询逻辑，应先在 main 通过测试，再同步到 intranet；两分支的 Provider enabled 状态、Vault 路径和 viewer 配置继续独立维护，不能借通用修复覆盖部署配置。

### 第二次内网同题回归与继续优化

trace `20260819_083733_79e1a696` 已完成并生成 evidence/claims，query-session duration 为 180538.073 ms，较前次 1068 秒墙钟跨度缩短约 83.1%。检索、读取与 provenance 仍低于 1 秒；`candidate-review`、第二轮 `evidence-gap-review` 和 `answer-synthesis` 合计 179913.524 ms，占受控时长 99.65%。因此下一轮继续只优化 compact candidate 覆盖与模型工具纪律，不增加普通查询的验证、检索或判级步骤。

本轮候选跨文档覆盖已经生效，但前三个不同文档各取一条后，剩余位置又按全局分数回填，紧凑窗口仍可能错过高相关文档的第二个互补章节。改为对前三个高相关文档按轮次交错选择：先取每个文档的最佳章节，再取各自第二章节，直到填满既有窗口；候选数量不增加，脚本 I/O 不增加。

同一回归中，模型在第二次有效 inspection 前查看完整候选、触发 inline Python 审批、探测 CLI help、两次猜测 selector 并读取脚本源码。为消除这些往返，`begin` 的 compact response 增加简短 selection contract，Skill 明确要求：compact candidates 是首次 inspection 的完整操作输入；一次选择全部当前有用候选；精确章节只能逐字复用已返回的 `document_path` 并追加 `::section-id`；不得读取 sidecar/trace state、猜 document ID/短路径、探测 help 或源码。精确形式仍失败时直接从现有证据收口，不继续 selector 试错。

答案作用域只采用领域无关的合成引导：结论保留已检查证据本身表达的适用边界，必要时加一句简短限定；不为扩大答案范围额外检索，也不在脚本、Skill 或模板中编码任何具体领域、系统、标准、语言或参数规则。

## 原流程的主要耗时来源

原流程中常见的额外步骤包括：

- 每次重新加载操作说明和配置后再确认一次 Vault；
- 为新问题枚举近期 request/trace，判断是否存在可恢复任务；
- 在稳定脚本已经存在时仍探测 CLI `--help` 或读取脚本源码；
- 因 inline Python 被审批门拦截，再创建临时 formatter/helper 脚本；
- 先输出完整候选 JSON，再创建格式化工具压缩结果；
- 按章节、表格、规范和原始 PDF 串行读取，多次往返完成交叉验证；
- evidence、claim、tag、finish 分别调用并分别落盘；
- 每个小动作都生成一次模型规划和过程叙述；
- 后半段依赖模型主动记录阶段，容易被压缩为一个笼统步骤；
- trace 只有局部脚本耗时，缺少请求级总耗时及无法归因的时间。

这些步骤中，检索计算不是主导项。即使尚未启用 qmd-like-rag，也应优先优化编排和上下文，而不是先替换检索 Provider。

## 当前快速路径

```text
ordinary:   query -> finalize
explicit visual-verification policy: query --verification-required -> verify -> optional ready-carrier visual check -> finalize
```

### `query`

一次完成：

- 创建新 trace，不枚举与当前问题无关的旧 trace；
- 并行运行可选 coarse recall 与 hierarchical routing；
- 融合、去重和排序候选；
- 在 trace sidecar 保存完整结果；
- 在脚本内部自动 inspect 排名前三个候选，候选不足三个时全部 inspect；
- 只向模型返回 evidence packets 和动态的最小合成契约，不返回候选列表。

qmd-like-rag 未配置、被禁用或暂时不可用时，coarse route 记为 disabled/unavailable，hierarchical route 继续工作，不额外进入故障排查循环。

### 内嵌 `inspect`

模型不再执行候选选择。脚本在 sidecar 保留完整 provenance，向模型批量返回紧凑内容：

- 完整 section-owned ranges；
- 关联的 governed outputs；
- 表格/图片 Markdown 与 verification assets；
- manifest、ledger、source-map 和 QA 状态；
- 原始 PDF 路径、页码和 viewer URL。
- verification readiness 及唯一支持的下一步。

当前性能优先策略只允许组合 `query` 内的一次 `inspect`；兼容命令的第二次 inspection 和所有 supplement 都由脚本返回 `blocked -> finalize`，不得继续搜索或调试 Skill。若首轮证据不足，直接以 `incomplete` 和一个实质 unresolved 收口。pass-quality Bundle 是默认内部提取载体；内容类型和 Bundle QA flag 本身不要求视觉原页核验。若 Bundle/control metadata 明确标记 QA、警告、歧义或不完整，普通查询直接限定或降级结论；只有用户明确要求视觉审计时才进入 `verify` 路径。

### `finalize`

普通路径只提交 claims、packet 引用和短 conclusion；脚本补齐固定 decision 字段。存在硬阻断或显式核验要求时才提交完整动态字段。脚本从 evidence catalog 自动继承路径、版本、章节、页码和原始 PDF，生成 ASCII evidence/claim ID，再原子化写入最终状态和 Markdown trace。`finalize` 在顶层返回已渲染的 `final_response`，最后一次模型续写只逐字转交该字段，不重新阅读证据或组织答案。Hermes 0.17.0 没有 Skill 级 `return_direct`，因此不修改 Hermes 的前提下不能彻底删除这次续写。多题最后一题使用 `--close-request` 原子校验请求并返回 capsules。

## 具体优化机制

### 减少工具往返

- 用 `query_session.py` 聚合原来分散的 start、retrieve、read、evidence、claim 和 finish 操作；
- 正常查询不读取稳定脚本源码，不探测 CLI help；
- 不为结果查看创建临时 Python formatter；
- 不为每条 evidence/claim 单独启动进程和写一次状态；
- 不创建或 patch 临时 manifest，正常 finalize 直接使用 `--decision-json`；
- 多问题仍逐题完成，以维持一题一 trace 的治理边界。

### 减少模型上下文

- `query` 不向模型返回候选列表，完整 fused scope 保留在 sidecar；
- 中文查询产生的重叠 n-gram 只保留最长且不同的少量命中词；
- trace Markdown 只展示最高优先级候选，诊断详情保留在 JSON；
- `inspect` 只读取已选择候选，不把整个 Vault 或全部候选送回模型。
- 每题完成后只保留 answer capsule 用于多题汇总；trace sidecar 保留完整 provenance 和 source ranges，源内容可从登记范围重建，不重复保存或传输整包正文。
- answer capsule 对来源去重，claims 只保留 `source_ids`，避免同一 PDF/页码在每条 claim 中重复。

一次真实大 Vault 基准中，初始检索输出从 35,204 bytes 降到 4,893 bytes，减少约 86.1%。

### 减少文件与状态 I/O

- hierarchical locator 对每个文档只读取一次 `document.md`，全部章节复用内存行；
- hierarchical locator 在固定 compact window 内先为最多三个高相关文档各保留一个强候选，再按 title/path 优先、content/document 次优的新增 query-term 覆盖选择剩余章节。这样不把 top 5 扩为 top 8、不增加 I/O，却能让同一文档中排名稍低但回答另一问题维度的章节越过重复候选；
- 原始 PDF 只从 Vault 内安全解析；外部 ingest 路径保留为诊断元数据，嵌套 `10_Raw` 副本按文件名和可用 SHA-256 确认；
- route trace 事件批量追加；
- finalization 在一次状态写入中记录 evidence、claims、events、metrics 和完成状态；
- 完整检索结果写 sidecar，不在模型与工具之间重复传输。

finalize packet 同时返回通用的 minimum-sufficient decision contract：每条 claim 必须对应必要的提问维度，同证据支持的紧密相关参数应合并；作用域、适用性或证据边界优先附着为简短 qualification，不默认成为独立 claim；只有实质影响正确性或使用方式的问题进入 `unresolved`；结论只做一次简短综合，不逐条复述 claims。该契约不按领域、系统、标准、语言或参数硬编码，也不新增模型调用。脚本不对语义冗余做强制拒绝，避免误判后触发额外 finalize 重试。

trace `20260824_083500_bc7fe661` 使用旧版 `65ad59e` 时在未启用视觉核验的普通查询中把 inspected packet `P1` 填入 `verified_evidence_refs`，第一次 finalize 被严格门禁拒绝，随后约 23 秒才完成修正重试。根因是模型把“inspect/读取证据”叙述成“核验”，而通用 decision 示例又默认展示了非空 verified refs。修复后，inspect 按当前 trace 状态返回动态 verification contract：`inspect_grants_verified_status: false`；未请求视觉核验时唯一合法值为 `verified_evidence_refs: []` 且不得生成 `page-asset-verification` 事件；请求视觉核验时仍只允许实际查看过 registered carrier 的引用。普通示例同步改为空列表，finalize 对误填返回明确错误但不静默降级或吞掉错误。该修复不增加调用、检索或验证步骤。

trace `20260824_090251_a3624b82` 使用动态 verification contract 后没有 finalize 重试，但模型把限定查询对象的词组误当成独立答案维度，首轮 inspect 扩大到四个章节，并按旧规则因工程参数/表格额外读取完整 `evidence-levels.md`。修复继续保持领域无关：requested facet 仅指用户要求输出的属性、值、条件、比较或动作；subject qualifier 只缩小作用域，不占候选槽位或生成 claim。inspect 同时根据 packet 的内容质量、source-map、原件/页码、截断和资产 QA 返回紧凑 `evidence_level_contract`。普通 pass-quality packet 使用内联四级规则直接 finalize；只有具体 QA/provenance trigger，或实际冲突、答案相关歧义、gap 才读取完整 reference。该规则不解析问题关键词，不包含系统、标准、章节、语言或参数特例，也不增加调用。

trace `20260824_101544_421650ea` 首次进入 120 秒目标（107942.854 ms），但三个 packet 的 source-map `warn` 仍触发了完整 evidence-level reference 读取，普通 finalize 又因可选 inspect event 引用未使用 handles 而失败并重试。性能优先策略进一步改为 fail-open diagnostics：非 failed 的 `warn`、`pending`、`qa_required`、`ambiguous`、`incomplete`（包括表格/图片标签）可直接作为 `source-backed` 使用，不触发 reference 读取；无正文、原件/页码缺失、答案内容截断、failed/unavailable-class 状态、真实来源冲突和显式视觉核验未完成仍是硬门禁。普通动态 event contract 要求 `events: []`，禁止为满足可选 event 引用而扩大 claim/evidence。规则只读取 packet 控制状态和内容存在性，不包含任何领域或问题特例。

后续内网模型曾把 agent-facing `candidate_count` 的完整融合总数与仅返回前五条的 `candidates` 数组误判为工具输出截断，继而尝试 inline `python3 -c` 和临时脚本读取完整列表。修复消除这一契约歧义：agent-facing `candidate_count` 只表示实际返回数，完整融合数量仅留在 trace；返回 `candidate_window_complete: true` 和 `producer_output_truncated: false`；移除 agent-facing route/fusion 的候选总数，限制可选标签、页码、路由和 warning 长度，并在固定字符预算内打包最多五条。若下游显示确实在 JSON 中途截断，唯一恢复路径是用已返回 trace ID 调用无 selector 的 `inspect` 默认窗口，禁止读取完整 trace、运行 inline Python 或写 helper script。该策略不解析问题、领域、文档或章节内容。

trace `20260825_024105_c3e20311` 在 108982.548 ms 内稳定完成，未再读取完整 trace/reference、运行 helper、重试 finalize 或误用视觉核验；但首轮 inspect 的三个 packet 中一个未用于 claim，另一个只生成未请求的通用对比，真正补齐请求属性的同文档详细章节需要第二轮 inspect，耗费 34946.923 ms 的模型选择时间。通用修复分两层：locator 在 section 路由时扣除已由 document identity 命中的主题词，限制重叠 n-gram 膨胀，并在文档多样性之前给最强文档一个能增加 query-language coverage 的互补章节槽位；模型侧增加 `candidate_purpose_gate` 和 `claim_pruning_gate`，证据存在不再构成 inspect/claim 理由，删除后仍完整回答请求的比较、背景、适用性或运行内容必须省略。规则只比较查询词覆盖和请求输出完整性，不包含系统、规范、章节、语言或参数特例。

### 2026-08-25 答案合成输入优化

trace `20260825_031033_61bf03fd` 只有一次 inspect 和一次成功 finalize，但总计 107468.615 ms 中 `answer-synthesis` 占 97433.471 ms。这个阶段不是“最终中文答案输出”计时：旧边界从 inspect 完成持续到 finalize 调用，混合了工具结果传输、模型服务排队、证据包阅读、推理、decision JSON 生成和调用准备，因此不能仅凭该数值认定模型在写答案。旧 trace 又没有 packet 字符数与 decision 字符数，无法判断主要压力来自证据输入还是决策输出。

本轮采用通用、仅影响 agent delivery 的压缩，不改变候选、证据登记、source ranges、页码、原始 PDF 或 claim-evidence 门禁：

1. `inspect` 对全部所选 packet 的 agent-facing 副本设置统一字符总预算，不再允许每个章节、资产和 governed artifact 分别消耗完整上限；该预算最初为 18,000，当前提高到 30,000，以降低关键上下文被摘录的概率；
2. 普通非视觉路径先移除重复资产正文及仅用于视觉审计的 carrier 字段；仍超预算时，按查询词覆盖保留 Markdown 命中块及相邻上下文，并用通用 omission marker 标记中间省略；
3. `delivery_excerpted` 只表示传输副本被压缩，不能替代或触发 `content_truncated`。完整 provenance 与 source ranges 继续登记在 trace，finalize 仍按 packet handle 继承；
4. 新增 diagnostic `evidence-packet-delivery` 事件及 `full_packet_chars`、`agent_packet_chars`、`saved_chars`、content 分类字符数、去重/摘录数和预算满足状态；finalize 另记录 `decision_input_chars`；
5. answer-synthesis 计时改为在 delivery copy 准备完成后开始，其含义明确为“agent 收到证据后到 decision 到达 finalize”的综合区间。finalize contract 同时要求最小有效 decision，但不对语义冗余做脚本拒绝，避免额外重试。

30,000 是当前总体交付预算而非每份文档配额；CLI 提供最低 4,000 的测试/诊断覆盖，但普通流程不应为了恢复已省略背景而读取 trace。预算从 18,000 放宽是因为现有 trace 未显示字符数是主要耗时来源，而过度摘录会增加模型怀疑证据不完整、转向补查的风险。摘录算法只使用查询词匹配、块邻接、字段类型权重和精确重复检测，不包含系统、规范、章节、语言或参数特例。长 packet 回归测试验证 5,000 字符预算下关键查询值和表格行仍保留，同时 trace 中登记压缩前后指标。

### 2026-08-25 回归稳定版与单轮硬门禁

对 `20260825_063514_bea55057` 与 `20260825_063950_14c20554` 的复盘表明，进一步压缩 inspect 输出并没有稳定降低端到端时间。前者总计约 132 秒，其中候选阶段约 38.5 秒、answer-synthesis 约 93.4 秒；后者总计约 295 秒，首轮证据之后又发生缺口判断、supplement、精确 selector 失败和第二轮 inspect 路径，新增证据并未形成有效主张。第二次最主要的性能损失来自模型在首轮证据后重新规划，而非候选数量或 agent-facing 字符数本身。

因此当前实现撤销 `21f0928`（main 对应 `ec078f9`）的 compact inspect response contract，以 intranet `53aab47`（main 对应 `97f36d5`）的稳定行为为基础，只保留以下领域无关的性能门禁：

1. `begin` 把实际返回的 compact candidates 逐项登记为唯一 inspection window；完整 fused scope 与 projection 继续只用于 trace 审计，不能成为后续选择来源；
2. `inspect` 的数字 rank、section ID 和 `document_path::section-id` 都只在该窗口内解析，窗口外的已知精确章节也拒绝；
3. 每个 trace 只允许一次 batched inspect，第二次调用确定性返回 `blocked -> finalize`；
4. `supplement` 保留为兼容命令，但不执行检索，确定性返回 `blocked -> finalize`；
5. 首轮证据足够时立即 completed finalize；不足时立即 incomplete finalize，并只记录影响答案的 material unresolved；
6. agent evidence 总预算放宽为 30,000 字符。速度收益依赖消除第二轮规划、检索和 inspect，而不是继续压缩证据文本。

这些规则不识别具体系统、规范、问题、语言或章节，也不给模型判断“是否值得再检索”的自由；其代价是首轮 compact window 漏检时召回率会下降。这个取舍是当前内网性能优先策略的显式边界，后续应通过相同问题、模型和 Vault 的 A/B trace 验证，而不是通过运行时 supplement 补偿。

Windows Vault 经 `/mnt/c` 被 WSL 访问时，多文件索引读取仍可能产生明显的跨文件系统开销；这不是 `/opt/data/...` Linux 本地 intranet Vault 的同类路径。若 main 的该场景成为生产目标，应增加单文件聚合索引或 Provider-side cache，而不是牺牲候选完整性。

## 自动 trace 与计时

`query-session/v2`、trace schema 1.5 自动记录以下阶段：

- scope retrieval；
- candidate review；
- document reading；
- table/figure resolution；
- provenance resolution；
- single-pass guardrail（仅当模型误发第二次 inspect 或 supplement 时记录，不执行检索）；
- answer synthesis；
- claim-evidence mapping；
- page/asset verification（声明 original asset verified 时必须提供）。

trace 同时记录：

- query-session 开始、结束和总耗时；
- primary stages 的 accounted duration；
- 总耗时与已核算阶段之差，即 unaccounted duration；
- 每个事件的 `started_at`、`ended_at`、`duration_ms`；
- attempted routes 与 effective routes，disabled/unavailable route 不冒充实际检索路线；
- evidence 和 claim 的 `recorded_at`；
- command count 与 inspection count。
- Hermes session ID、message ID 与 platform，由运行时环境自动继承。
- 多题 request summary 的 controlled request duration（首个 begin 到最后一个 finalize）。
- begin 在任何检索前拒绝未分组的明显多问题输入、同 request 并发 trace、重复/跳号 index 和 count 冲突；finalize 原子拒绝空 claim 文本及未知 decision 字段。
- request summary 记录 expected/recorded count、重叠次数和重叠时长，不能再把实际交错执行表述为 sequential。

计时边界从 `query_session.py query` 调用开始，到 `finalize` 开始最终持久化为止。用户请求到第一条工具调用之前、最后工具返回到答案发出之后、模型服务排队以及审批等待，需要通过 Hermes session ID 和 `agent.log` 补齐，不能伪装成脚本阶段耗时。

## 不因性能优化而改变的约束

- 原始 PDF 仍是用户可见证据来源；Bundle、索引、ledger 和 trace 只是导航或核验载体；
- 查询不得重建或同步 Provider；
- governed Vault 内容保持只读，仅允许写当前非权威 query trace；
- pass-quality Bundle 默认可作为内部提取载体；视觉核验只由显式用户/审计要求触发，具体 QA/歧义状态直接体现在证据等级和限定语中；
- Provider 不可用不得阻塞已有 hierarchical fallback；
- 可独立回答的多个问题仍按顺序分别完成 trace。

## 验证结果

当前实现完成了以下自动验证：

- 两调用组合 query/finalize workflow 的集成测试；
- bootstrap 一次返回规则、配置和 verification capability；
- registered verification carrier 的单次准备与无 carrier/renderer 时的 fast-fail；
- 同 request 的 open-trace 拒绝、连续 question index/count 和 `--close-request`；
- unknown 顶层 decision/claim field 拒绝、unknown event field 的 `extensions` 兼容保留及 `unresolved_items` 兼容继承；
- begin 实际返回窗口的持久化与窗口外精确章节拒绝；
- 显式声明需要视觉核验的证据未核验时拒绝 `clear`/`source-backed`；
- inspect provenance 自动继承及 ASCII evidence/claim ID；
- 第二次 inspect 与所有 supplement 确定性阻止并导向 finalize；
- 去重 answer capsule、finalize 自动请求收口与 request-summary 汇总；
- Hermes session/message 环境继承；
- 无效 claim 导致 finalization 整体失败且不产生部分写入；
- required-stage coverage 检查；
- attempted/effective route 区分；
- evidence/claim 时间戳和请求级计时输出；
- 紧凑候选和 n-gram 限制；
- main 与 intranet 分支使用同一单轮门禁回归测试；
- Skill 结构校验通过；
- 所有 Python 入口保持 Git executable mode `100755`。

本地 Windows 侧真实 Vault 的 scope retrieval 约为 0.39 秒。通过 `/mnt/c` 由 WSL 读取同一 Vault 的结果受跨文件系统多文件访问影响，不能代替 intranet Linux 本地 Vault 的验收数据。

## Intranet A/B 验收方法

在部署前后使用相同问题、相同模型、相同 Vault 状态和相同证据要求，至少各执行五次，分别记录冷启动和稳态结果。建议至少统计：

| 指标 | 目的 |
| --- | --- |
| request-to-answer wall time | 判断用户实际等待时间 |
| query-session duration | 判断 Skill 可控制的端到端时间 |
| accounted/unaccounted duration | 识别模型思考、等待或漏记阶段 |
| script command count | 确认普通路径是否稳定为两次 |
| inspection count | 识别候选质量或证据包是否导致返工 |
| retrieval duration by route | 判断 qmd-like-rag/hierarchical 的真实贡献 |
| returned bytes/tokens | 判断上下文压缩效果 |
| evidence level and coverage gaps | 防止速度提升来自证据降级 |

验收时应同时检查最终答案、trace Markdown、trace JSON sidecar 和 Hermes `agent.log`。如果 query-session 很短而 request-to-answer 仍很长，下一轮应优先优化模型提示和输出；如果 retrieval route 占主导，再优化索引或 Provider。

## 后续优化优先级

1. 在 intranet 完成同题 A/B，取得真实 request-to-answer P50/P95；
2. 对 unaccounted duration 最大的样本关联 Hermes 日志，区分模型思考、服务排队、审批和答案输出；
3. 若首个自动窗口经常留下证据缺口，调整通用排序或 evidence packet，而不是恢复模型候选选择或第二轮检索；
4. qmd-like-rag 启用后保持与 hierarchical route 并行，并用 route timing 判断收益；
5. 若 Windows-mounted Vault 成为长期运行路径，构建单文件聚合索引或 Linux-local Provider cache；
6. Hermes prompt 的独立合成上下文和工具结果直返都需要编排器支持；本轮按约束不修改 Hermes，仅保留为需要单独授权的架构选项。

本设计的核心不是让模型“更快地执行原来的十几个步骤”，而是取消不必要的步骤，把稳定、可验证的工作下沉到确定性脚本中，并让可观测性自动产生。
