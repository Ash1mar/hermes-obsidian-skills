# Controlled Query 时间优化设计

## 文档定位

本文件是 `hermes-obsidian-controlled-query` 的维护者参考，记录真实性能事故、可控调用边界、实现策略和回归验收。保留在该 Skill 的 `references/` 下是合适的：内容直接约束 `query_session.py`、trace 和 evidence decision，不属于 Vault 业务知识，也不应在普通查询中默认加载。只有性能优化、trace 诊断或 Skill 维护时才读取。

若将来出现跨多个 Skill 共用的 Hermes API/VPN、模型服务或全局 prompt-compaction 运维规范，应另建仓库级运行手册，并从这里链接；不要把本文件复制到 Vault 或 Provider 运维文档中造成双份事实来源。

## 背景与目标

一次典型的单问题证据查询耗时约 6 分 45 秒。原 trace 表明候选检索本身只占约 1.4 秒；主要时间消耗来自模型与工具之间的多轮编排、反复读取和格式化、逐项证据处理，以及最后逐条写 trace。因此优化顺序是：

1. 先减少模型—工具往返和返回上下文；
2. 再压缩文件读取和 trace 写入次数；
3. 最后补齐后半段阶段记录与请求级计时，避免可观测性反过来增加查询步骤。

普通单问题查询的结构目标是三次 query-session 调用；工程参数确需查看原页时，增加一次确定性 `verify` 准备和一次视觉核验。每个用户请求另有一次 `bootstrap`，多题最后一次 finalize 通过 `--close-request` 同时返回汇总。端到端验收以 intranet 上同题 A/B 为准，暂定普通证据查询 P50 不超过 180 秒，并以接近或低于 120 秒为进一步目标。脚本耗时、模型思考时间、网络/API 重试、审批等待和回答输出时间必须分开报告。

## 2026-08-18 真实双题事故

Hermes session `20260818_162026_79f45a` 使用 GPT-5.5 回答两个问题，实际用户端到端约 349.2 秒；controlled request 只记录 262.64 秒。17 次模型调用全部成功，没有 API 自动重试，模型 API 总等待约 292 秒。两次 RAG/层级并行检索工具执行约 47 秒，因此本轮高耗时的主因是调用轮数和大上下文，不是 VPN 失败重试。

### PDF 核验试错链

第一题涉及喷水强度和喷头参数，`inspect` 返回了转换章节和原 PDF 页码，但没有可直接查看的 evidence image 或 viewer URL；Hermes 运行环境也没有预先声明可用的视觉渲染能力。Skill 只规定“核验原页”，没有规定能力不可用时的单次停止条件。模型因此自主尝试：

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
5. **P5 · 严格 decision schema**：未知字段报错，`unresolved_items` 作为兼容别名；工程参数若未完成原页核验，不得使用 `clear`/`source-backed`；verified ref 必须对应带实际 carrier 路径的完成事件。
6. **P6 · 上下文压缩**：inspect 返回紧凑 QA/verification packet，完整 provenance 留在 sidecar；answer capsule 使用去重 `sources` 和 claim `source_ids`；最后收口不重新加载 evidence packet。

新工程参数路径为：

```text
bootstrap（每请求一次）
-> begin
-> inspect
-> verify（一次；不可用即停止）
-> visual check（仅 ready）
-> finalize [--close-request]
```

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
ordinary:   begin -> inspect -> finalize
explicit visual-verification policy: begin --verification-required -> inspect -> verify -> optional ready-carrier visual check -> finalize
```

### `begin`

一次完成：

- 创建新 trace，不枚举与当前问题无关的旧 trace；
- 并行运行可选 coarse recall 与 hierarchical routing；
- 融合、去重和排序候选；
- 在 trace sidecar 保存完整结果；
- 只向模型返回最多五个紧凑候选和必要路由计时。

qmd-like-rag 未配置、被禁用或暂时不可用时，coarse route 记为 disabled/unavailable，hierarchical route 继续工作，不额外进入故障排查循环。

### `inspect`

模型一次选择回答所需的全部候选。脚本在 sidecar 保留完整 provenance，向模型批量返回紧凑内容：

- 完整 section-owned ranges；
- 关联的 governed outputs；
- 表格/图片 Markdown 与 verification assets；
- manifest、ledger、source-map 和 QA 状态；
- 原始 PDF 路径、页码和 viewer URL。
- verification readiness 及唯一支持的下一步。

只有出现真实缺口、冲突或漏检来源时才允许第二次 `inspect`。工程值、公式、表格或图片内部信息仍需按证据规则核验原页，不能为了省时降低证据质量。

### `finalize`

模型一次提交只含 claims、inspect packet 引用、结论、`unresolved` 和可选核验事件的 decision。`unresolved_items` 仅作为兼容别名；任何其他未知字段会被拒绝。脚本从 evidence catalog 自动继承路径、版本、章节、页码和原始 PDF，生成 ASCII evidence/claim ID，再原子化写入最终状态和 Markdown trace。任一记录无效时不留下半完成的 finalization。多题最后一题使用 `--close-request` 原子校验请求并返回 capsules。

## 具体优化机制

### 减少工具往返

- 用 `query_session.py` 聚合原来分散的 start、retrieve、read、evidence、claim 和 finish 操作；
- 正常查询不读取稳定脚本源码，不探测 CLI help；
- 不为结果查看创建临时 Python formatter；
- 不为每条 evidence/claim 单独启动进程和写一次状态；
- 不创建或 patch 临时 manifest，正常 finalize 直接使用 `--decision-json`；
- 多问题仍逐题完成，以维持一题一 trace 的治理边界。

### 减少模型上下文

- `begin` 只返回紧凑候选，完整 fused scope 保留在 sidecar；
- 中文查询产生的重叠 n-gram 只保留最长且不同的少量命中词；
- trace Markdown 只展示最高优先级候选，诊断详情保留在 JSON；
- `inspect` 只读取已选择候选，不把整个 Vault 或全部候选送回模型。
- 每题完成后只保留 answer capsule 用于多题汇总，完整 packet 留在 trace sidecar。
- answer capsule 对来源去重，claims 只保留 `source_ids`，避免同一 PDF/页码在每条 claim 中重复。

一次真实大 Vault 基准中，初始检索输出从 35,204 bytes 降到 4,893 bytes，减少约 86.1%。

### 减少文件与状态 I/O

- hierarchical locator 对每个文档只读取一次 `document.md`，全部章节复用内存行；
- route trace 事件批量追加；
- finalization 在一次状态写入中记录 evidence、claims、events、metrics 和完成状态；
- 完整检索结果写 sidecar，不在模型与工具之间重复传输。

Windows Vault 经 `/mnt/c` 被 WSL 访问时，多文件索引读取仍可能产生明显的跨文件系统开销；这不是 `/opt/data/...` Linux 本地 intranet Vault 的同类路径。若 main 的该场景成为生产目标，应增加单文件聚合索引或 Provider-side cache，而不是牺牲候选完整性。

## 自动 trace 与计时

`query-session/v2`、trace schema 1.4 自动记录以下阶段：

- scope retrieval；
- candidate review；
- document reading；
- table/figure resolution；
- provenance resolution；
- evidence-gap review（仅第二次 inspect）；
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

计时边界从 `query_session.py begin` 调用开始，到 `finalize` 开始最终持久化为止。用户请求到第一条工具调用之前、最后工具返回到答案发出之后、模型服务排队以及审批等待，需要通过 Hermes session ID 和 `agent.log` 补齐，不能伪装成脚本阶段耗时。

## 不因性能优化而改变的约束

- 原始 PDF 仍是用户可见证据来源；Bundle、索引、ledger 和 trace 只是导航或核验载体；
- 查询不得重建或同步 Provider；
- governed Vault 内容保持只读，仅允许写当前非权威 query trace；
- 表格、公式和工程参数必须执行必要的原页/verification asset 核验；
- Provider 不可用不得阻塞已有 hierarchical fallback；
- 可独立回答的多个问题仍按顺序分别完成 trace。

## 验证结果

当前实现完成了以下自动验证：

- 三调用 decision workflow 的集成测试；
- bootstrap 一次返回规则、配置和 verification capability；
- registered verification carrier 的单次准备与无 carrier/renderer 时的 fast-fail；
- 同 request 的 open-trace 拒绝、连续 question index/count 和 `--close-request`；
- unknown decision field 拒绝及 `unresolved_items` 兼容继承；
- 显式声明需要视觉核验的证据未核验时拒绝 `clear`/`source-backed`；
- inspect provenance 自动继承及 ASCII evidence/claim ID；
- supplement 后缺少第二次 inspect 时拒绝 finalize；
- 去重 answer capsule、finalize 自动请求收口与 request-summary 汇总；
- Hermes session/message 环境继承；
- 无效 claim 导致 finalization 整体失败且不产生部分写入；
- required-stage coverage 检查；
- attempted/effective route 区分；
- evidence/claim 时间戳和请求级计时输出；
- 紧凑候选和 n-gram 限制；
- main 分支全套测试 58 项通过；
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
| script command count | 确认普通路径是否稳定为三次 |
| inspection count | 识别候选质量或证据包是否导致返工 |
| retrieval duration by route | 判断 qmd-like-rag/hierarchical 的真实贡献 |
| returned bytes/tokens | 判断上下文压缩效果 |
| evidence level and coverage gaps | 防止速度提升来自证据降级 |

验收时应同时检查最终答案、trace Markdown、trace JSON sidecar 和 Hermes `agent.log`。如果 query-session 很短而 request-to-answer 仍很长，下一轮应优先优化模型提示和输出；如果 retrieval route 占主导，再优化索引或 Provider。

## 后续优化优先级

1. 在 intranet 完成同题 A/B，取得真实 request-to-answer P50/P95；
2. 对 unaccounted duration 最大的样本关联 Hermes 日志，区分模型思考、服务排队、审批和答案输出；
3. 若经常发生第二次 inspect，调整 compact candidates、文档路由词或 evidence packet，而不是盲目扩大首次上下文；
4. qmd-like-rag 启用后保持与 hierarchical route 并行，并用 route timing 判断收益；
5. 若 Windows-mounted Vault 成为长期运行路径，构建单文件聚合索引或 Linux-local Provider cache；
6. 若多题 API 输入上下文仍线性增长，在 Hermes prompt 组装层将已经 finalize 的 begin/inspect tool output 替换为 answer capsule；磁盘会话与 trace 原文保持不变。

本设计的核心不是让模型“更快地执行原来的十几个步骤”，而是取消不必要的步骤，把稳定、可验证的工作下沉到确定性脚本中，并让可观测性自动产生。
