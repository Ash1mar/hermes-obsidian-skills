# Controlled Query 性能优化记录

## 文档定位

本文件是 `hermes-obsidian-controlled-query` 的维护者参考，用于按时间顺序记录性能迭代中出现的问题、原因判断、通用解决思路和当前边界。

本文件不属于 Vault 业务知识，也不应在普通查询中默认加载。只有性能优化、运行记录诊断或 Skill 维护时才读取。

所有规则必须保持领域中立。历史样本只用于验证通用机制，不在这里记录或复述具体运行标识、业务系统、用户问题、标准、文档、章节或参数，也不得把它们写入代码、配置、提示或测试作为行为引导。

## 当前结论

迭代过程中最稳定的结论是：检索和文件读取通常不是主要耗时，完整模型调用、模型—工具往返、证据返回后的重新规划和 decision 合成才是主要成本。

优化优先级因此固定为：

1. 减少完整模型调用和工具往返；
2. 阻止第二轮检索、重复 inspect、selector 调试和运行记录恢复；
3. 只向模型交付首轮证据和最小动态合成契约；
4. 在不损失关键证据的前提下控制上下文，而不是持续缩小字符预算；
5. 自动记录计时和 provenance，避免为了可观测性增加模型步骤。

普通单问题当前目标路径为：

```text
bootstrap（每个用户请求一次）
-> query（创建记录、召回并自动 inspect 首个有界窗口）
-> finalize
-> 返回 finalize.final_response
```

只有用户或明确审计要求原页视觉检查时，才在 `query` 与 `finalize` 之间增加一次确定性的 `verify` 和一次实际视觉检查。参数、公式、表格、图片或普通 Bundle QA 状态本身不触发视觉路径。

## 迭代时间线

### 第一阶段：识别多轮编排是首要瓶颈

早期测试中，单问题经常需要数分钟。模型会把检索、候选选择、逐段读取、证据登记、原页核验、claim 写入、结论生成和收尾分别处理，每个小步骤都可能形成一次完整模型调用。

当原页视觉能力不可用时，模型还会尝试多种替代工具、检查运行环境、枚举文件并重新搜索转换内容。这些尝试增加了大量往返，却不能完成真正的视觉核验。

同一时期还出现了：

- 把 `inspect` 误认为已经完成视觉 `verify`；
- 核验不可用时缺少单次停止条件；
- decision 字段不一致导致 unresolved 被忽略；
- 多问题交错执行，污染单题计时；
- 请求汇总需要额外模型轮次。

这一阶段采用的通用策略是：

1. 增加显式、领域无关的 `--verification-required`；
2. `verify` 只使用已登记载体，并只返回有限状态；不可用或失败时立即停止；
3. `inspect` 只代表证据读取和登记，绝不授予 verified 状态；
4. 顶层 decision 和 claim 保持严格校验，必要兼容字段显式归一化；
5. 多问题严格逐题完成，最后一次 finalize 同时完成请求收口；
6. `bootstrap` 一次返回规则、部署配置、会话关联和核验能力，取消重复搜索。

### 第二阶段：处理补证、重试和路径异常

减少基础步骤后，主要浪费转移到失败后的重新规划。常见表现包括：

- finalize schema 不匹配后重新生成完整 decision；
- supplement 与 inspect 顺序错误；
- selector 失败后继续猜测其他写法；
- 读取稳定 CLI help 或脚本源码；
- 退回临时文件搜索；
- 来源路径解析失败后修改已部署 Skill；
- 同一证据重复 inspect 后仍继承旧 provenance；
- 候选被单一来源的重复词占满；
- inspect 和 supplement 次数只有文字限制，没有代码门禁。

问题可分为三个责任层次：

- **确定性代码问题**：路径解析、catalog 刷新、候选多样性、schema 和调用次数门禁；
- **模型编排问题**：重复搜索、猜 selector、修改运行代码、未及时 incomplete 收口；
- **部署与观测问题**：可选 Provider 状态和会话关联不足，影响候选质量或耗时归因。

这一阶段采用的通用策略是：

1. 原始来源只允许从 Vault 内安全解析，外部路径只保留为诊断信息；
2. 嵌套原件通过精确身份和可用校验信息确认，歧义时返回 unresolved；
3. 同版本、同 section 的 evidence handle 可以刷新 provenance，版本变化时拒绝静默覆盖；
4. 候选排序增加跨文档多样性和同文档互补内容机会；
5. query-session 自动记录失败并返回唯一安全的下一步；
6. 为 inspect 和 supplement 增加脚本硬门禁；
7. 查询期间禁止修改 Skill、放宽路径约束或继续实现级调试。

这些修复消除了无限补查和路径修补链，耗时从十几分钟级降至数分钟级，但候选选择、补证判断和答案合成仍占主要时间。

### 第三阶段：明确 compact candidates 是完整操作输入

后续测试暴露出一组相似的小问题：模型没有把脚本返回值视为完整操作输入。

常见表现包括：

- 为查看更完整候选而读取 sidecar 或完整运行记录；
- inline Python 被审批后改写临时 helper script；
- 探测稳定 CLI help、猜 selector 形状、读取源码；
- 把候选总数与实际返回数组长度不同误认为输出被截断；
- 为恢复未展示字段而再次读取来源；
- 把限定查询对象的词组误当成独立输出维度。

对应的通用策略是：

1. compact window 是首次证据读取的完整操作边界；
2. 完整 fused scope 只留在运行记录中，不对模型开放；
3. agent-facing count 只表示实际返回数量；
4. 明确区分生产者主动压缩、证据传输摘录和下游显示截断；
5. selector 只能来自已返回窗口，不允许猜测或恢复窗口外内容；
6. 禁止读取完整记录、运行 inline Python、写临时 formatter/helper、探测 help 或源码；
7. requested facet 只表示用户要求输出的属性、值、条件、比较或动作；subject qualifier 只缩小范围；
8. 证据存在本身不构成扩大 inspect 或 claim 的理由。

候选排序也由简单全局分数调整为通用覆盖策略：先保留强候选和文档多样性，再允许最强来源获得一个能增加查询词覆盖的互补 section 槽位。该策略不扩大固定窗口，也不编码领域知识。

### 第四阶段：补齐 verification、evidence 和 event 动态契约

候选流程稳定后，重复失败主要来自 decision contract 不够动态：

- inspected packet 被误填为 verified evidence；
- 普通警告或未完成状态触发完整 evidence-level reference 读取；
- 可选 event 引用未进入 claim 的 packet，导致 finalize 失败；
- 模型为满足 event 合法性而扩大 evidence 或 claim；
- 非关键 QA 标签被当作必须停止的错误。

这一阶段将规则改为随当前记录动态返回：

1. 未请求视觉核验时，`verified_evidence_refs` 固定为空且不生成 verification event；
2. 只有实际查看过已登记载体的引用才能进入 verified evidence；
3. 非 failed 的 `warn`、`pending`、`qa_required`、`ambiguous`、`incomplete` 作为非阻断诊断；
4. 正文、原件和页码可解析时，非阻断诊断可直接作为 `source-backed` 使用；
5. 无正文、原件或页码缺失、答案内容截断、failed/unavailable 状态、真实来源冲突及显式视觉核验未完成继续作为硬门禁；
6. 普通查询 `events: []`，禁止为了 event 合法性增加无关 claim；
7. 普通状态使用内联 evidence policy，不额外读取完整参考文档。

这些修改减少了 reference 读取和 finalize 重试，并保持证据安全边界不变。

### 第五阶段：确认 answer-synthesis 是后半段大头

在检索和文档读取降到秒级以内后，`answer-synthesis` 仍可能持续几十秒甚至更长。该阶段不是单纯的最终答案输出，而是从模型收到 evidence packets 到 decision 到达 finalize 的综合区间，可能包含：

- 模型服务排队和首 token 延迟；
- 阅读 evidence packets；
- 内部推理与重新评估候选；
- 组织 claims、qualification、unresolved、events 和 conclusion；
- 生成 decision JSON 并准备下一次工具调用。

在模型服务本身不可调整的条件下，pipeline 能做的是减少输入、减少 decision 字段并阻止重新规划。

本阶段尝试了 agent-facing evidence 压缩：

- 所有 packet 使用统一总预算；
- 普通路径移除重复资产正文及视觉审计字段；
- 超预算时保留查询命中块和相邻上下文；
- `delivery_excerpted` 与源读取 `content_truncated` 分离；
- 自动记录压缩前后字符数、内容分类、去重和摘录指标；
- answer-synthesis 计时从 delivery copy 完成后开始。

实践表明字符量不是主要矛盾。预算过紧会让模型怀疑证据不完整，并触发补查或恢复行为。因此总预算最终放宽，性能收益继续依赖减少模型调用，而不是进一步压缩正文。

### 第六阶段：回归稳定基础并强制单轮证据处理

一次激进压缩和合约调整没有稳定降低端到端时间。部分测试在首轮证据后重新判断缺口，进入 supplement、selector 失败和第二轮 inspect，新增证据又没有形成必要主张，反而显著变慢。

因此实现回到已经验证的稳定基础，只保留以下硬门禁：

1. 首轮 compact window 是唯一 inspection window；
2. 每个记录只允许一次 batched inspect；
3. `supplement` 保留兼容入口但始终返回 `blocked -> finalize`；
4. 首轮足够则 completed，首轮不足则 incomplete；
5. 不允许模型自由决定第二轮检索；
6. evidence 总预算保持较宽松水平，避免因为摘录过度产生恢复行为。

这个取舍明确以性能稳定性优先。首轮偶发漏检应通过通用排序和 evidence packet 改进，而不是在运行时恢复 supplement。

### 第七阶段：删除候选选择模型调用

单轮门禁后，普通流程仍包含一次模型候选选择。由于一次完整模型调用通常就在数十秒量级，删除这次调用比优化毫秒级检索更有效。

在不修改外部编排器的条件下，完成了以下改造：

1. 新增组合 `query`，在同一脚本进程内完成 begin、候选融合和首个有界窗口的自动 inspect；
2. 自动 inspect 前三条，少于三条时全部读取；
3. 候选列表不再返回给模型，从流程中删除候选选择调用；
4. `query` 只返回 evidence packets、packet handles、字符指标和小型 `synthesis_contract`；
5. 普通无阻断路径只要求模型生成 `claims` 和短 `conclusion`；
6. 固定 status、evidence level、events、verification 和 unresolved 由脚本补齐；
7. `finalize` 顶层返回已渲染的 `final_response`，最后续写只负责转交；
8. 清空默认领域路由词，并清理代码、配置、文档和测试中的具体业务示例。

组合路径把受控查询阶段从分钟级进一步降低到约半分钟级。检索、自动候选 review、文档读取和 provenance 仍只占很小比例，剩余时间几乎全部集中在唯一的答案合成模型轮次。

外部编排器当前没有 Skill 级工具结果直返语义，工具结果仍会进入下一次模型续写。因此仅靠 Skill 不能彻底删除 finalize 后的模型调用，也不能为 synthesis 创建真正独立的干净上下文。本轮不修改外部编排器，这两项只保留为需要单独授权的架构选项。

### 当前稳定阶段：效果可接受，小问题暂缓

最近多次内网回归表明：

- 组合 `query -> finalize` 稳定执行；
- 没有第二轮 supplement 或 inspect；
- 无正文 packet 可以被模型排除；
- 检索和读取阶段维持在很低水平；
- 受控查询总时长已进入可接受区间。

目前仍观察到三个小问题：

- 一个不可用 packet 可能让整个 synthesis contract 进入较大的 qualified 模式，即使其余 packet 已足够；
- 模型偶尔生成超出用户必要输出的相关 claim；
- 模型偶尔以恢复完整输出为由尝试向临时目录写 helper script，尽管规则已经禁止。

这些问题尚未造成稳定的检索返工或 finalize 失败，当前暂不修改稳定路径。后续如果数据表明有必要，应采用领域无关方式处理：按 packet 分离 usable/excluded refs、增加明确的 operational-output-complete 标志，并继续通过最小 claim 契约引导收口。真正从权限层禁止任意 shell helper 需要外部编排或审批策略，不属于当前 Skill-only 范围。

## 当前实现边界

### `query`

一次完成：

- 创建运行记录；
- 并行运行可选 coarse recall 与 hierarchical routing；
- 融合、去重和排序；
- 将完整候选及拒绝原因保存到 sidecar；
- 自动 inspect 首个有界窗口；
- 批量登记 provenance、原始来源、页码、表格/图片和 QA；
- 在统一总预算内返回 agent-facing evidence copy；
- 返回当前记录的最小 synthesis contract。

可选 Provider disabled/unavailable 时不进入故障排查循环，hierarchical route 继续工作。查询期间不得同步或重建 Provider。

### `finalize`

普通路径只提交最小 claims 与 conclusion。脚本负责：

- 校验 packet handles 与 claim–evidence map；
- 补齐固定 decision 字段；
- 从 evidence catalog 继承版本、section、页码和原始来源；
- 生成稳定 evidence/claim IDs；
- 原子写入 state 和 Markdown；
- 返回去重 answer capsule 和顶层 `final_response`。

脚本不对语义冗余做强制拒绝，避免误判后触发额外 finalize 重试。最小 claim 仍由动态契约引导。

## 责任边界

| 类别 | 典型问题 | 当前处理方式 |
| --- | --- | --- |
| query-session 代码 | 路径解析、catalog 刷新、候选窗口、调用次数、schema、计时 | 确定性脚本修复并自动测试 |
| Skill 与模型编排 | 重复搜索、补证、helper、selector 调试、冗余 claim | 缩短流程、返回动态契约、设置硬停止条件 |
| 外部编排器 | 工具结果无法直接返回、无法创建真正干净 synthesis 上下文、任意 shell 权限 | 当前不修改；需要单独授权 |
| 模型服务 | 排队、首 token、内部 reasoning 速度 | pipeline 无法直接控制，只能减少调用和输入 |
| Provider 与部署 | enabled 状态、索引和运行时路径 | 部署独立配置，不写入通用查询逻辑 |

## 自动记录与计时

query-session 自动记录：

- preflight、coarse recall、hierarchical routing、fusion 和 scope retrieval；
- automatic candidate review、document reading、asset 与 provenance resolution；
- evidence packet delivery 的压缩前后字符指标；
- answer synthesis、claim-evidence mapping 和可选 visual verification；
- command count、inspection count、attempted/effective routes；
- accounted/unaccounted duration；
- evidence/claim 时间戳和请求级计时；
- 可用时继承会话和消息关联。

计时从 `query_session.py query` 开始，到 `finalize` 验证并持久化为止。以下时间不应伪装成脚本阶段：

- 用户消息到第一次工具调用；
- bootstrap 前后的模型调用；
- finalize 返回后的最终模型续写；
- 模型服务排队、网络等待和审批等待。

要获得真正 request-to-answer 时间和完整模型调用数，必须关联外部运行日志。会话关联缺失时，只能对 query-session 内部区间作确定性判断。

## 不因性能优化而改变的约束

- 原始 PDF 仍是用户可见证据来源；Bundle、索引、ledger 和运行记录只是导航或核验载体；
- governed Vault 内容保持只读，只允许写当前非权威查询记录；
- 查询不得重建或同步 Provider；
- Provider 不可用不得阻塞已有 hierarchical fallback；
- 视觉核验只由明确用户或审计要求触发；
- 硬 evidence-chain blocker 不得因性能要求而静默放行；
- 可独立回答的多个问题仍逐题完成；
- 各部署分支共用领域无关逻辑，Vault 路径、viewer 和 Provider 状态保持独立；
- 不在代码、Skill、配置、文档或测试中加入具体问题作为行为引导。

## 验证与验收

当前自动验证覆盖：

- 组合 `query -> finalize` 两调用 workflow；
- automatic first-window inspect；
- 第二次 inspect 和 supplement 硬阻止；
- bootstrap 规则、配置与 verification capability；
- evidence delivery 总预算和关键内容保留；
- 动态 verification/evidence/event contract；
- Vault 内 provenance 继承和路径安全；
- strict decision/claim schema 与原子 finalize；
- answer capsule、`final_response` 和 request closure；
- attempted/effective route、计时和字符指标；
- 各部署分支的领域中立回归；
- Python 入口的 Git executable mode。

部署前后 A/B 应在相同模型、Vault 状态和证据要求下统计冷启动与稳态数据，至少记录：

| 指标 | 目的 |
| --- | --- |
| request-to-answer wall time | 判断用户真实等待 |
| query-session duration | 判断 Skill 可控区间 |
| answer-synthesis duration | 判断剩余模型调用成本 |
| command/inspection count | 识别流程是否重新膨胀 |
| retrieval duration by route | 判断 Provider 的真实贡献 |
| agent/full packet chars | 判断上下文压缩是否有效 |
| decision input chars | 判断收口是否保持最小 |
| evidence level and gaps | 防止速度来自证据降级 |

## 暂缓事项

当前组合路径已经达到可接受效果。以下优化暂缓，只有回归数据再次显示稳定收益时才继续：

1. 按 packet 分离 usable/excluded evidence，避免无效 packet 扩大整个 decision；
2. 增加更明确的 output-complete/no-recovery contract，进一步减少 helper script 偶发行为；
3. 继续改进通用 claim pruning，但不增加语义硬拒绝和 finalize 重试；
4. 启用 Provider 后用实际 route timing 判断收益，不预设单一路线必然更快；
5. 外部编排器级独立 synthesis context 或工具结果直返，仅在获得单独授权后评估。

本轮迭代的核心不是让模型更快地执行原来的许多步骤，而是取消不必要的步骤，并把稳定、可验证、领域无关的工作下沉到确定性脚本中。
