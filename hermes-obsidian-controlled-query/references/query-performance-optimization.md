# Controlled Query 时间优化设计

## 背景与目标

一次典型的单问题证据查询耗时约 6 分 45 秒。原 trace 表明候选检索本身只占约 1.4 秒；主要时间消耗来自模型与工具之间的多轮编排、反复读取和格式化、逐项证据处理，以及最后逐条写 trace。因此优化顺序是：

1. 先减少模型—工具往返和返回上下文；
2. 再压缩文件读取和 trace 写入次数；
3. 最后补齐后半段阶段记录与请求级计时，避免可观测性反过来增加查询步骤。

普通单问题查询的结构目标是三次脚本调用；工程参数确需查看原页时，可以增加一次视觉核验。端到端验收以 intranet 上同题 A/B 为准，暂定普通证据查询 P50 不超过 180 秒，并以接近或低于 120 秒为进一步目标。脚本耗时、模型思考时间、审批等待和回答输出时间必须分开报告。

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
begin -> inspect -> optional original-page visual check -> finalize
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

模型一次选择回答所需的全部候选。脚本批量返回：

- 完整 section-owned ranges；
- 关联的 governed outputs；
- 表格/图片 Markdown 与 verification assets；
- manifest、ledger、source-map 和 QA 状态；
- 原始 PDF 路径、页码和 viewer URL。

只有出现真实缺口、冲突或漏检来源时才允许第二次 `inspect`。工程值、公式、表格或图片内部信息仍需按证据规则核验原页，不能为了省时降低证据质量。

### `finalize`

模型一次提交只含 claims、inspect packet 引用、结论、未解决项和可选核验事件的 decision。脚本从 evidence catalog 自动继承路径、版本、章节、页码和原始 PDF，生成 ASCII evidence/claim ID，再原子化写入最终状态和 Markdown trace。任一记录无效时不留下半完成的 finalization。

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
- inspect provenance 自动继承及 ASCII evidence/claim ID；
- supplement 后缺少第二次 inspect 时拒绝 finalize；
- answer capsule 与 request-summary 汇总；
- Hermes session/message 环境继承；
- 无效 claim 导致 finalization 整体失败且不产生部分写入；
- required-stage coverage 检查；
- attempted/effective route 区分；
- evidence/claim 时间戳和请求级计时输出；
- 紧凑候选和 n-gram 限制；
- 全套测试 51 项通过；
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
