# 摄取代码的原生验收与正式流程验证

本合同定义两种原生验收方式。回归通过证明代码检查通过；部署候选证明安装了对应代码；
阶段验收通过证明当前阶段实际完成。三者分别记录，八片通过也不代表全流程完成。

## 选择验证方式

| 方式 | 适用范围 | 执行和后续处理 |
| --- | --- | --- |
| 隔离原生验收 | 未获正式实测授权的 Vault；故障注入、破坏性与负向测试 | 使用隔离 Vault、Hermes Home、Kanban 和 Gateway；通过后按分支合同发布部署 |
| 正式流程分段验证 | 用户明确选定的 Vault；本 workspace 默认选定 HBTest2 | 回归通过后安装已提交候选，由真实 workflow 执行各段；审阅证据后沿用已完成结果继续同一流程 |

正式实测的范围由 workspace 规则及当前用户请求决定，不把具体 Vault 路径、actor、模型、
workflow/batch/slice ID 或开发者版本标签硬编码到通用 Skill、修复逻辑或 prompt。
仅更新规则不授权启动、部署或继续；用户要求暂停时保持暂停。

## 候选部署之前

1. 在 `main` 完成通用修复，运行适当回归、Skill 结构、内嵌库一致性与入口 Git `100755`
   检查，将候选提交并记录完整 SHA。已通过检查且候选内容未变化时，不为切换验证方式重复
   完整回归；有新修改时补做受影响检查。
2. 核对部署范围、实际安装文件指纹及活动操作员、Gateway、reconciler 和 worker。
   停止会读取被替换代码的进程，确认旧 worker 不再提交；其他 workflow 在候选实测期间
   不能读取候选或并行写入相同范围。共享运行环境无法满足这个边界时，先保持阻塞。
3. 核对之前部署的完整 SHA 可从远端恢复；主机独有或未提交代码先整合提交。
   记录仓库、旧 SHA、候选 SHA、文件指纹、检查结果、目标和运行环境。
   只暂存新候选并在使用后清理，不保留旧代码副本。
4. 记录来源原件哈希、当前 workflow/revision、已有来源结果及有效产物引用，核对
   已钉住的 worker 契约与候选是否兼容。只覆盖安装目录不会更新旧 workflow 的模板快照。
   需要换契约时使用受支持恢复接口；缺少安全迁移接口时保持阻塞。
5. 正式实测允许在完整原生八片验收前部署这个候选，但部署状态必须是“待原生验证”。
   不以此声称一般发布或 intranet 已通过，也不自动改写其他 Vault 或 Provider 配置。

## 正式流程与持久化暂停

由 Hermes 操作员会话读取权威状态并提交已获授权的调度请求，独立原生 Kanban worker
执行来源准备、exact-plan、Pass 和所有后续领域工作。Codex 代替用户启动会话、观察和
验收，不能用测试 helper 代做这些任务，也不能手工完成卡片或修改 ledger。

采用 `execution_mode=auto_full`、`provider=sync`，并在 workflow 创建时持久化以下策略：

```json
"pause_after": ["exact_plan", "canary", "pass", "checkpoint_1", "build_finalize", "checkpoint_2", "release_sync"]
```

| 暂停点 | 本段需核对的真实证据 | 必须保持未运行的后继 |
| --- | --- | --- |
| `exact_plan` | 各来源有合法结果；完整 ready UnitRef 覆盖；当前 batch/task 的实际序列化预算；原生准备和规划运行证据 | 全部 Pass |
| `canary` | 八个钉住 slice 的原生运行、绑定预校验成功结果及完整有效 Pass；canary 报告与暂停 | 剩余 Pass 及全部下游 |
| `pass` | 全部计划任务和 slice 的 Pass 与引用覆盖 | Reduce |
| `checkpoint_1` | 检查点通过报告及经契约验证的决定 | Build Finalize |
| `build_finalize` | 完整 build run 和产物引用 | Vault Finalize release planning |
| `checkpoint_2` | release plan 验证报告及经契约验证的决定 | Release apply |
| `release_sync` | 已提交 release；实际 Provider generation、配置/模型指纹及同步状态 | 最终 acceptance |

每段结束核对实际暂停的 boundary、evidence digest、报告和后继未派发/未写入证据，
将结果交给用户后停止。单纯 resume、sync、重启 Gateway 或“验收通过”不能释放暂停。
下一次显式授权只释放当前一个边界，使用 Skill 的 `continue-workflow` 和最新权威身份；
同一请求重试沿用同一授权 ID。最终 acceptance 也须在 `release_sync` 之后单独继续。

正式八片是完整 batch 的首段。八片通过后保持 `canary` 暂停，之后从同一 ledger 继续
剩余 slice；有效已完成结果不会为了再跑一遍“正式流程”被清空。来源、预算或契约变化
需要重新验证受影响产物，其余产物仅在当前哈希、身份和合同验证通过时复用。

## 原生证据与失败处理

- 证据必须来自本轮当前部署候选、绑定的真实 worker 和当前 batch；保存原生 task/run/session
  关联、工具调用及结果、请求摘要、产物引用和哈希。八个 worker 均须有绑定的
  `batch-pass --validate-only` 调用及成功工具结果，并核对随后写入的草稿是通过预校验的版本。
  worker 文字声明、直接 helper 测试或历史隔离结果不能替代这些证据。
- 原生会话和拒绝证据在进程/临时环境退出前保存到持久审计位置。正式 ledger 允许按契约
  写入本阶段结果；核对原件未变化、写入均在授权范围，其他 workflow 与未开放阶段不受影响。
  不套用隔离测试的“生产 ledger 完全不变”作为正式实测成功条件。
- 转换的明确文档失败必须有绑定来源哈希和 backend 的诊断，按契约保留来源缺口；
  环境、模型、安全拒绝、绑定、参数及未知错误保留为执行阻塞。来源缺口使最终结果为
  partial，不能报告全部来源成功；八片不足八个有效 slice 时仍未通过八片验收。
- 阻塞时保存真实命令、工具结果和失败报告，暂停受影响流程。在 `main` 做通用修复并运行
  受影响检查，再按部署及恢复合同更新候选；记录每个候选 SHA 对应的证据。
  不绕过安全检查，不修改绑定或暂停，不删除证据，不另建 workflow 来规避失败。
  修复需要的 cancel/repair/resume 或来源 reset 必须有相应授权与契约支持。
- 正式 Vault 不做故意损坏产物、杀进程、伪造错误或修改看板的故障注入。锁、拒绝、取消、
  崩溃等负向回归保留在隔离测试；正式流程只观察自然发生的状态并按合同恢复。

## 发布边界与隔离工具

正式实测满足以上准备、精确预算、八片、预校验和暂停证据后，可作为相应的原生发布门槛，
不再要求对相同候选和输入重复一遍隔离八片。仍需完成 `main` 验证/推送、同一干净工作树
merge 到 `intranet`、保留分支配置、intranet 验证/推送，再做获授权的其他部署。
未经八片验证的候选不能作为一般发布；后来发生回归或证据失效时重新验证受影响范围。

`hermes-source-units/tools/accept_governed_canary.py` 保留为隔离验收入口，不是正式流程
启动或继续入口。隔离方式仍须使用实际 Hermes Python、同文件系统的测试 Vault，以及
隔离 Home/Kanban/Gateway PID；暂停改动用 `--verify-pauses`，草稿预校验改动用
`--verify-preflight`。正式方式通过受支持 Hermes 调度执行并只读收集等价证据；
不把正式流程冒充该脚本通过的隔离报告，不修改脚本来自动越过正式暂停。

操作员手册只记录可重复的操作步骤，运行结果、失败原因和验收结论单独保存。
详细运行契约见 [Orchestrator operations](hermes-obsidian-governed-ingest-orchestrator/references/operations.md)，
发布顺序见 [分支维护合同](BRANCH_MAINTENANCE.md)。
