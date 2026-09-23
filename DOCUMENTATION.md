# 文档索引

本文档说明 `hermes-obsidian-skills` 仓库内现行文档的职责、阅读顺序和维护边界。审计基线为 2026-09-23 的 `origin/main`（`5be21da`）；后续提交按各分支实际 revision 生效。历史验收、时间点调研和性能时间线保存在本地 Git 忽略目录 `legacy docs/`。

## 推荐阅读顺序

1. 先读 [`README.md`](README.md)，了解仓库能力、六个 Skill、短命令和 Provider 边界。
2. 做设计、评审、交付或跨团队沟通时读 [`docs/OFFICIAL_TECHNICAL_SPECIFICATION.md`](docs/OFFICIAL_TECHNICAL_SPECIFICATION.md)。
3. 设计多机构材料、文档身份或业务版本时读 [`docs/architecture/0001-weknora-inspired-document-governance.md`](docs/architecture/0001-weknora-inspired-document-governance.md)。
4. 需要理解端到端关系时读 [`charts.md`](charts.md)。
5. 执行具体任务时只加载对应 Skill 的 `SKILL.md`，再按其中的路由读取必要 reference。
6. 部署或排障检索 Provider 时读 [`RETRIEVAL_PROVIDER_OPERATIONS.md`](RETRIEVAL_PROVIDER_OPERATIONS.md)。
7. 安装、迁移或排查 MinerU WSL 环境时读 [`MINERU_WSL_ENVIRONMENT_RUNBOOK.md`](MINERU_WSL_ENVIRONMENT_RUNBOOK.md)。

`SKILL.md` 是运行入口和边界契约；`references/` 保存较长的操作细则；`scripts/` 是实际执行入口。流程说明与脚本行为冲突时，应先核对当前分支、部署配置和测试，再修正文档或实现，不能在运行时临时发明替代流程。

## 仓库级文档

来源单元新体系的现行入口：[ADR-0003](docs/architecture/0003-source-unit-contracts.md)、[ADR-0004](docs/architecture/0004-knowledge-identity-and-finalize.md)、[ADR-0005](docs/architecture/0005-shared-chunk-engine.md)、[ADR-0006](docs/architecture/0006-release-driven-retrieval-projection.md)、[共享包规范](hermes-source-units/README.md)、[演进计划](docs/SOURCE_UNITS_EVOLUTION_PLAN.md)和 [Governed Ingest Orchestrator](hermes-obsidian-governed-ingest-orchestrator/SKILL.md)。Provider 0.5 已部署在 main WSL，真实 release generation、材料检索和 intranet 联调仍待验收。

| 文档 | 内容与用途 |
| --- | --- |
| [`README.md`](README.md) | 仓库总览：六个 Skill、Hermes slash aliases、目录结构、qmd-like-rag、验证方法及 MinerU、图片 Bundle、MarkItDown 集成入口。 |
| [`DOCUMENTATION.md`](DOCUMENTATION.md) | 当前文档索引，说明每份文档讲什么、应该何时阅读，以及哪些仓库外资料仅作关联参考。 |
| [`docs/OFFICIAL_TECHNICAL_SPECIFICATION.md`](docs/OFFICIAL_TECHNICAL_SPECIFICATION.md) | 项目级官方技术规范：统一定义六个 Skill、数据权威、schema/协议、持久化摄取、发布、查询、分支部署、QA、安全和验收标准。 |
| [`docs/architecture/0001-weknora-inspired-document-governance.md`](docs/architecture/0001-weknora-inspired-document-governance.md) | 已接受且阶段 1、2 已实现的架构决策：借鉴 WeKnora 分层建立最小文档身份、来源、业务版本、隔离和存储引用合同，并以 JSON repository 为数据库迁移做准备。 |
| [`docs/architecture/0003-source-unit-contracts.md`](docs/architecture/0003-source-unit-contracts.md) | SourceUnit 身份、坐标、核心/上下文、文件仓库和复制式运行契约；记录 P0–P2 实践结论。 |
| [`docs/architecture/0002-general-knowledge-construction.md`](docs/architecture/0002-general-knowledge-construction.md) | 已接受的通用知识构建方法、证据与版本治理边界；保留原始决策理由，现行执行以 Skill 为准。 |
| [`docs/architecture/0004-knowledge-identity-and-finalize.md`](docs/architecture/0004-knowledge-identity-and-finalize.md) | 稳定 subject/page identity、Build Finalize、Vault Finalize、独立 Finalize Skill 和数据库后置决策。 |
| [`docs/architecture/0005-shared-chunk-engine.md`](docs/architecture/0005-shared-chunk-engine.md) | 共享 Chunk Engine、P2.1 UnitSet 身份及回退/诊断决策。 |
| [`docs/architecture/0006-release-driven-retrieval-projection.md`](docs/architecture/0006-release-driven-retrieval-projection.md) | P5 Release 驱动的 SourceUnit/knowledge-page 检索投影、真实 tokenizer、索引代次、Query 精确回读和部署边界。 |
| [`docs/SOURCE_UNITS_EVOLUTION_PLAN.md`](docs/SOURCE_UNITS_EVOLUTION_PLAN.md) | 从 P0 契约到 P7 正式重建的当前阶段计划、子任务与验收边界。 |
| [`docs/WIKI_KNOWLEDGE_CONSTRUCTION_GUIDE.md`](docs/WIKI_KNOWLEDGE_CONSTRUCTION_GUIDE.md) | 现行来源单元到知识页的操作与人工判断指南。 |
| [`docs/HERMES_WIKI_FROM_FILES_TO_KNOWLEDGE.md`](docs/HERMES_WIKI_FROM_FILES_TO_KNOWLEDGE.md) | 当前从文件到来源单元、知识 release 和检索的解释性说明。 |
| [`charts.md`](charts.md) | Mermaid 端到端流程图：`main`/`intranet` 环境差异、建库、摄取、Lint、单遍 Query Session、Provider 与 Vault 的读写关系。 |
| [`BRANCH_MAINTENANCE.md`](BRANCH_MAINTENANCE.md) | 双分支维护合同：共享变更先进入 `main`，再 merge 到 `intranet`；定义受保护配置、冲突处理、验证和推送顺序。 |
| [`RETRIEVAL_PROVIDER_OPERATIONS.md`](RETRIEVAL_PROVIDER_OPERATIONS.md) | 检索 Provider 运维说明：三层配置、Query/Finalize 独立开关、release sync 门禁和验证命令。 |
| [`MINERU_WSL_ENVIRONMENT_RUNBOOK.md`](MINERU_WSL_ENVIRONMENT_RUNBOOK.md) | MinerU 在 WSL2 中的安装、模型缓存、离线配置、迁移、pipeline/hybrid 验证、CUDA/vLLM 排障经验。它是环境运行手册，不是 Bundle 摄取步骤。 |

## Governed Ingest Orchestrator 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-governed-ingest-orchestrator/SKILL.md`](hermes-obsidian-governed-ingest-orchestrator/SKILL.md) | 单请求建立持久工作流；模板钉住、状态/恢复、双人工检查点和 canary 派发总入口。 |
| [`references/workflow-contract.md`](hermes-obsidian-governed-ingest-orchestrator/references/workflow-contract.md) | Vault 权威记录、请求摘要、版本化模板、Kanban 投影和 canary allowlist。 |
| [`references/operations.md`](hermes-obsidian-governed-ingest-orchestrator/references/operations.md) | start/status/approve/resume/cancel、看板恢复与受限 canary 操作。 |
| [`references/worker-contracts.md`](hermes-obsidian-governed-ingest-orchestrator/references/worker-contracts.md) | worker 节点的输入钉住、允许写入、心跳及失败返回。 |
| [`references/compatibility-profiles.md`](hermes-obsidian-governed-ingest-orchestrator/references/compatibility-profiles.md) | 旧三阶段/六阶段提示的只读展示映射；不改变领域执行图。 |
| `references/workers/*.md` | 十二类版本化 worker 模板；启动时复制快照并按 SHA-256 钉住。 |

## Controlled Ingest 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-controlled-ingest/SKILL.md`](hermes-obsidian-controlled-ingest/SKILL.md) | 受控摄取总入口：保护 `10_Raw/`、识别新导入/恢复/续做/写回、执行 Bundle 与 ledger 门禁、发布 SourceUnits 和知识构建记录；不维护 Provider 索引。 |
| [`references/source-units.md`](hermes-obsidian-controlled-ingest/references/source-units.md) | UnitSet 原生阅读、精确预算、Pass slice、分层 Reduce 与 Build Finalize。 |
| [`references/knowledge-construction.md`](hermes-obsidian-controlled-ingest/references/knowledge-construction.md) | 未声明 SourceUnit 知识构建能力的旧 Vault 所用 v3 证据与页面规则；不用于 P5 Vault。 |
| [`references/workflow-ledger.md`](hermes-obsidian-controlled-ingest/references/workflow-ledger.md) | Vault 权威 ingest workflow 状态与两个审批点。 |
| [`references/kanban-adapter.md`](hermes-obsidian-controlled-ingest/references/kanban-adapter.md) | Kanban 投影、任务 lease、重建和领域结果对账。 |
| [`references/vault-structure.md`](hermes-obsidian-controlled-ingest/references/vault-structure.md) | Vault 目录、治理文件、原始层与衍生层的职责边界。 |
| [`references/concept-governance.md`](hermes-obsidian-controlled-ingest/references/concept-governance.md) | 概念注册、查重、候选概念、合并与升级边界，防止过度创建概念页。 |
| [`references/markitdown.md`](hermes-obsidian-controlled-ingest/references/markitdown.md) | 使用本地 MarkItDown 转换脚本处理非 Markdown、非复杂 PDF 来源时的输入、输出与质量约束。 |
| [`references/mcp-markitdown.md`](hermes-obsidian-controlled-ingest/references/mcp-markitdown.md) | MarkItDown MCP 的可选配置方式和安全边界；MCP 只负责格式转换，不负责知识判断。 |
| [`references/mineru-pdf-bundle.md`](hermes-obsidian-controlled-ingest/references/mineru-pdf-bundle.md) | MinerU PDF Bundle v2 的生成、目录结构、校验、章节范围、图表与证据层使用方式。 |
| [`references/mineru-output-review.md`](hermes-obsidian-controlled-ingest/references/mineru-output-review.md) | MinerU 原始输出的保留、人工复核、派生修订与候选 Bundle 再生成契约，并明确当前尚未实现的 review compiler 能力。 |
| [`references/image-bundle.md`](hermes-obsidian-controlled-ingest/references/image-bundle.md) | 扫描页、截图、图表和其他 image-only 来源的 Bundle v2/OCR 处理及 QA 限制。 |
| [`references/bundle-source-map-ledger.md`](hermes-obsidian-controlled-ingest/references/bundle-source-map-ledger.md) | source map 与 section ledger 的初始化、领取、修订号、状态转换、断点续做和 stale 对账。 |
| [`references/document-governance.md`](hermes-obsidian-controlled-ingest/references/document-governance.md) | 文档治理管理器的 revision、锁、审计、机构审批、登记、原子激活，以及 `ingest-start`/`ingest-finish` 与 Bundle 投影、ledger 准入链路。 |

## Knowledge Finalize 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-knowledge-finalize/SKILL.md`](hermes-obsidian-knowledge-finalize/SKILL.md) | P4 增量 Vault Finalize 的独立运行入口，负责 plan/apply/validate/status，并在 P5 由显式 release submission 触发可选 Provider sync。 |
| [`references/vault-finalize.md`](hermes-obsidian-knowledge-finalize/references/vault-finalize.md) | 来源贡献失效、页面 disposition、安全重定向、导航阻断、release manifest 与恢复规则。 |
| [`references/release-indexing.md`](hermes-obsidian-knowledge-finalize/references/release-indexing.md) | 已提交 release 的显式 Provider sync、检索状态与故障恢复边界。 |

## Controlled Query 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-controlled-query/SKILL.md`](hermes-obsidian-controlled-query/SKILL.md) | 受治理知识库的默认问答入口：每请求一次 bootstrap，每题执行 `query → finalize`；`query` 自动融合检索并检查首个紧凑窗口，必要时才走一次显式原页核验。 |
| [`references/query-workflow.md`](hermes-obsidian-controlled-query/references/query-workflow.md) | `query_session.py` 的当前命令接口、evidence packet、最小 decision JSON、单遍边界、多题串行和失败回退。 |
| [`references/query-tracing.md`](hermes-obsidian-controlled-query/references/query-tracing.md) | Query trace、sidecar、事件、Evidence/Claim 映射、请求分组、计时与调试/兼容路径。 |
| [`references/coarse-retrieval.md`](hermes-obsidian-controlled-query/references/coarse-retrieval.md) | qmd-like-rag 粗召回的调用边界、候选语义、Provider 不可用时的行为和禁止在查询期间写索引的规则。 |
| [`references/Hierarchical_search.md`](hermes-obsidian-controlled-query/references/Hierarchical_search.md) | 基于 query-index、文档名、标题与父子章节路径的分层定位，以及与粗召回结果的融合。 |
| [`references/evidence-levels.md`](hermes-obsidian-controlled-query/references/evidence-levels.md) | `clear`、`source-backed`、`needs-qa`、`incomplete` 等证据状态及其使用条件。 |
| [`references/answer-format.md`](hermes-obsidian-controlled-query/references/answer-format.md) | 非简单答案的结构、原 PDF/页码引用、限定语、不确定性和 intranet 原文定位链接要求。 |

## Vault Bootstrap 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-vault-bootstrap/SKILL.md`](hermes-obsidian-vault-bootstrap/SKILL.md) | 初始化受治理 Vault 的入口，只负责建库，不负责摄取材料。 |
| [`references/profiles.md`](hermes-obsidian-vault-bootstrap/references/profiles.md) | `general`、`meeting` 与 `engineering` profile 的目录、模板、注册表和索引差异。 |
| [`references/script-usage.md`](hermes-obsidian-vault-bootstrap/references/script-usage.md) | `init_obsidian_vault.py` 的参数、模板复制、覆盖保护、预览与验证方法。 |
| [`references/governance-control-plane.md`](hermes-obsidian-vault-bootstrap/references/governance-control-plane.md) | engineering Vault 的身份、JSON 权威文件、维护边界、repository 合同和 SQLite/PostgreSQL 迁移准备。 |

## Vault Lint 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-vault-lint/SKILL.md`](hermes-obsidian-vault-lint/SKILL.md) | 只读 Vault 健康检查入口；不自动修复 Vault。 |
| [`references/profiles.md`](hermes-obsidian-vault-lint/references/profiles.md) | `post-ingest`、`query-ready`、`strict`、`qa-review` 四种检查强度和适用场景。 |
| [`references/rule-catalog.md`](hermes-obsidian-vault-lint/references/rule-catalog.md) | 目录、Bundle、ledger、source map、frontmatter、证据链、综合产物和 QA 边界的规则目录。 |
| [`references/output-contract.md`](hermes-obsidian-vault-lint/references/output-contract.md) | Lint JSON/Markdown 输出字段、状态、错误与警告的稳定接口。 |

## qmd-like-rag 文档与依赖锁

| 文件 | 内容与用途 |
| --- | --- |
| [`qmd-like-rag/README.md`](qmd-like-rag/README.md) | coarse-recall Provider 的运行边界、索引语料、`main`/`intranet` 部署、HTTP transport、安装和被移除的原型能力。 |
| [`qmd-like-rag/requirements.txt`](qmd-like-rag/requirements.txt) | 通用 Python 依赖约束；属于安装输入，不是操作手册。 |
| [`qmd-like-rag/requirements-gpu-cu130.txt`](qmd-like-rag/requirements-gpu-cu130.txt) | `main` 主机已验证的 CUDA 13.0 GPU 运行依赖锁；不适用于没有对应 GPU/CUDA 条件的主机。 |

## 相关但不是说明文档的目录

| 目录 | 作用 |
| --- | --- |
| `*/agents/openai.yaml` | Skill 展示名称和短描述等运行时元数据。 |
| `*/config/` | 分支、路由、Provider 和部署开关；运行时有效值还要与主机部署层和 Vault 控制面合并。 |
| `*/scripts/` | Skill 的可执行入口。脚本的 shebang、Git `100755` 模式和显式 `python3` 调用是部署契约。 |
| [`hermes-skill-bundles/`](hermes-skill-bundles/) | `/v-query`、`/v-ingest`、`/v-bootstrap`、`/v-lint` 的 Hermes bundle YAML；编排 Skill 目前没有短命令 bundle。 |
| [`mcp/`](mcp/) | 可选 MCP 配置示例，不保存实际凭据或主机私有配置。 |
| [`tests/`](tests/) 和 `qmd-like-rag/tests/` | Skill、脚本、Provider、打包模式和关键契约的回归测试。 |

## 仓库外关联资料（本索引仅说明，不修改）

以下文件位于 `hermes-obsidian-skills` 之外，本轮不属于可修改范围：

| 路径 | 关联内容 |
| --- | --- |
| `../ENVIRONMENT.md` | 当前 Windows/WSL、Hermes、MinerU、QMD、qmd-like-rag 和 Vault 存储边界的工作区事实源。 |
| `../AGENTS.md` | 工作区级执行约束，包括先读环境说明、检索存储合同和 Skill 脚本可执行模式要求。 |
| `../README-HERMES-WSL.md`、`../HERMES-USER-GUIDE.zh-CN.md` | Hermes WSL 部署与用户操作说明。 |
| `../Hermes+Obsidian受控知识库方案设计与实践总结.md` | 方案背景、早期实践、治理原则和阶段计划。 |
| `../doc/大专业计划-知识摄取方法与建设计划.docx` | 面向“大专业”建设的知识摄取方法、四模块分工、实践基础与后续计划。 |
| `../Hermes-*-Vault/README.md`、`AGENTS.md`、`_system/` | 各 Vault 实例自己的使用边界、模板、Prompt、控制面元数据和运行/验收记录；它们不是 Skill 源码文档。 |
| `../tmp/`、`../output/`、`../outputs/`、`../test0626.md` | 临时产物、测试输出和历史验证记录，不应作为当前 Skill 契约。 |

## 维护规则

- 功能入口、命令顺序或强制边界变化：先更新对应 `SKILL.md` 和直接 reference；影响系统架构、数据权威、兼容性或验收标准时同步更新官方技术规范，再更新 `README.md`、`charts.md` 与本索引。
- 分支部署差异：保存在分支配置和分支文档中；不要把某台主机的临时地址写成跨分支通用规则。
- 历史证据、阶段验收、时间点调研和性能时间线放入本地 `legacy docs/`；该目录由 `.gitignore` 排除，不能用 `git add -f` 强制加入远程库。已存在的 Git 历史仍保留旧版本，日常提交不追踪这些本地文件。
- 当前操作路径以对应 `SKILL.md` 和直接 reference 为准；不要从本地历史快照恢复旧命令作为正常路径。
- Provider 模型、索引和缓存是主机数据面；Vault 只保存可审计的配置与状态记录。
- 生成的 Vault 报告、转换正文和测试输出不纳入本仓库文档索引的维护对象。
