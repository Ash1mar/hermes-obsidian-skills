# 文档索引

本文档说明 `hermes-obsidian-skills` 仓库内各类文档的职责、阅读顺序和维护边界。审计基线为 2026-09-01 获取的远程状态：本地 `main` 与 `origin/main` 一致，本地 `intranet` 与 `origin/intranet` 一致。

## 推荐阅读顺序

1. 先读 [`README.md`](README.md)，了解仓库能力、四个 Skill、短命令和 Provider 边界。
2. 做设计、评审、交付或跨团队沟通时读 [`docs/OFFICIAL_TECHNICAL_SPECIFICATION.md`](docs/OFFICIAL_TECHNICAL_SPECIFICATION.md)。
3. 设计多机构材料、文档身份或业务版本时读 [`docs/architecture/0001-weknora-inspired-document-governance.md`](docs/architecture/0001-weknora-inspired-document-governance.md)。
4. 需要理解端到端关系时读 [`charts.md`](charts.md)。
5. 执行具体任务时只加载对应 Skill 的 `SKILL.md`，再按其中的路由读取必要 reference。
6. 部署或排障检索 Provider 时读 [`RETRIEVAL_PROVIDER_OPERATIONS.md`](RETRIEVAL_PROVIDER_OPERATIONS.md)。
7. 安装、迁移或排查 MinerU WSL 环境时读 [`MINERU_WSL_ENVIRONMENT_RUNBOOK.md`](MINERU_WSL_ENVIRONMENT_RUNBOOK.md)。

`SKILL.md` 是运行入口和边界契约；`references/` 保存较长的操作细则；`scripts/` 是实际执行入口。流程说明与脚本行为冲突时，应先核对当前分支、部署配置和测试，再修正文档或实现，不能在运行时临时发明替代流程。

## 仓库级文档

| 文档 | 内容与用途 |
| --- | --- |
| [`README.md`](README.md) | 仓库总览：四个 Skill、Hermes slash aliases、目录结构、qmd-like-rag、验证方法及 MinerU、图片 Bundle、MarkItDown 集成入口。 |
| [`DOCUMENTATION.md`](DOCUMENTATION.md) | 当前文档索引，说明每份文档讲什么、应该何时阅读，以及哪些仓库外资料仅作关联参考。 |
| [`docs/OFFICIAL_TECHNICAL_SPECIFICATION.md`](docs/OFFICIAL_TECHNICAL_SPECIFICATION.md) | 项目级官方技术规范：统一定义系统范围、架构、组件职责、数据权威、schema/协议、四条工作流、分支部署、QA、安全、故障降级、兼容性与验收标准。 |
| [`docs/architecture/0001-weknora-inspired-document-governance.md`](docs/architecture/0001-weknora-inspired-document-governance.md) | 已接受且阶段 1、2 已实现的架构决策：借鉴 WeKnora 分层建立最小文档身份、来源、业务版本、隔离和存储引用合同，并以 JSON repository 为数据库迁移做准备。 |
| [`charts.md`](charts.md) | Mermaid 端到端流程图：`main`/`intranet` 环境差异、建库、摄取、Lint、单遍 Query Session、Provider 与 Vault 的读写关系。 |
| [`BRANCH_MAINTENANCE.md`](BRANCH_MAINTENANCE.md) | 双分支维护合同：共享变更先进入 `main`，再 merge 到 `intranet`；定义受保护配置、冲突处理、验证和推送顺序。 |
| [`RETRIEVAL_PROVIDER_OPERATIONS.md`](RETRIEVAL_PROVIDER_OPERATIONS.md) | 检索 Provider 运维说明：仓库默认、主机部署、Provider 主机三层配置，query/ingest 开关，启用门禁和验证命令。 |
| [`MINERU_WSL_ENVIRONMENT_RUNBOOK.md`](MINERU_WSL_ENVIRONMENT_RUNBOOK.md) | MinerU 在 WSL2 中的安装、模型缓存、离线配置、迁移、pipeline/hybrid 验证、CUDA/vLLM 排障经验。它是环境运行手册，不是 Bundle 摄取步骤。 |

## Controlled Ingest 文档

| 文档 | 内容与用途 |
| --- | --- |
| [`hermes-obsidian-controlled-ingest/SKILL.md`](hermes-obsidian-controlled-ingest/SKILL.md) | 受控摄取总入口：保护 `10_Raw/`、识别新导入/恢复/续做/写回、选择转换路线、执行 Bundle 与 ledger 门禁、路由治理产物并维护可选检索索引。 |
| [`references/vault-structure.md`](hermes-obsidian-controlled-ingest/references/vault-structure.md) | Vault 目录、治理文件、原始层与衍生层的职责边界。 |
| [`references/concept-governance.md`](hermes-obsidian-controlled-ingest/references/concept-governance.md) | 概念注册、查重、候选概念、合并与升级边界，防止过度创建概念页。 |
| [`references/markitdown.md`](hermes-obsidian-controlled-ingest/references/markitdown.md) | 使用本地 MarkItDown 转换脚本处理非 Markdown、非复杂 PDF 来源时的输入、输出与质量约束。 |
| [`references/mcp-markitdown.md`](hermes-obsidian-controlled-ingest/references/mcp-markitdown.md) | MarkItDown MCP 的可选配置方式和安全边界；MCP 只负责格式转换，不负责知识判断。 |
| [`references/mineru-pdf-bundle.md`](hermes-obsidian-controlled-ingest/references/mineru-pdf-bundle.md) | MinerU PDF Bundle v2 的生成、目录结构、校验、章节范围、图表与证据层使用方式。 |
| [`references/mineru-output-review.md`](hermes-obsidian-controlled-ingest/references/mineru-output-review.md) | MinerU 原始输出的保留、人工复核、派生修订与候选 Bundle 再生成契约，并明确当前尚未实现的 review compiler 能力。 |
| [`references/image-bundle.md`](hermes-obsidian-controlled-ingest/references/image-bundle.md) | 扫描页、截图、图表和其他 image-only 来源的 Bundle v2/OCR 处理及 QA 限制。 |
| [`references/bundle-source-map-ledger.md`](hermes-obsidian-controlled-ingest/references/bundle-source-map-ledger.md) | source map 与 section ledger 的初始化、领取、修订号、状态转换、断点续做和 stale 对账。 |
| [`references/retrieval-indexing.md`](hermes-obsidian-controlled-ingest/references/retrieval-indexing.md) | 摄取完成后如何通过 adapter 增量同步 coarse-recall Provider，以及 Vault 控制面记录与主机索引数据的分离。 |
| [`references/document-governance.md`](hermes-obsidian-controlled-ingest/references/document-governance.md) | 文档治理管理器的 revision、锁、审计、机构审批、登记、追加来源、状态和原子激活命令。 |

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
| [`references/query-performance-optimization.md`](hermes-obsidian-controlled-query/references/query-performance-optimization.md) | Query Session 性能迭代记录、测量口径、已采用优化和保留问题。它包含历史流程，用于设计回顾，不替代当前 `SKILL.md` 与 `query-workflow.md`。 |

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
| [`hermes-skill-bundles/`](hermes-skill-bundles/) | `/v-query`、`/v-ingest`、`/v-bootstrap`、`/v-lint` 的 Hermes bundle YAML。 |
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
- 性能迭代历史：保留在 `query-performance-optimization.md`；当前操作路径只以 `SKILL.md` 和 `query-workflow.md` 为准。
- Provider 模型、索引和缓存是主机数据面；Vault 只保存可审计的配置与状态记录。
- 生成的 Vault 报告、转换正文和测试输出不纳入本仓库文档索引的维护对象。
