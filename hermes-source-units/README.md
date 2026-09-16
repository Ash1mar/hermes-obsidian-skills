# Hermes Source Units

版本 `0.4.0`，运行时第三方依赖为零，仅使用 Python 3.11+ 标准库。P2.1 的共享 Chunk Engine 提供 canonical SourceUnit；P3 在同一 UnitRef 上实现任务领取、阅读材料包、Pass 0/Pass 1..N、稳定知识身份、Reduce 页面修订和 Build Finalize；P4 实现增量 Vault Finalize。Provider 属于 P5。

## 交付与运行

- `src/hermes_source_units/schemas/contracts.json`：JSON Schema Draft 2020-12，所有 `$ref` 内联到同文件，不请求网络。
- `validation.py`：形状校验、局部约束及针对给定单元清单的引用关系检查。
- `chunk_engine/`：P2.1 的 profile、sections、atoms、strategies、overlap、validation 和 engine；不依赖 repository 或 Provider。
- `source_units.py`：`FileSourceUnitService`，实现 Markdown/Bundle v2 适配、确定性身份、预览、发布、验证、读取、token audit 和上下文组合。
- `knowledge_build.py`：`FileKnowledgeBuildService`，实现 P3 文件式工作账本、阅读包、Pass/Reduce、身份 registry、页面修订和可恢复 Build Finalize。
- `vault_finalize.py`：`FileVaultFinalizeService`，实现 P4 影响分析、贡献失效、导航/重定向投影、索引资格与 knowledge release。
- `interfaces.py`：P2 `SourceUnitBuilder` / `SourceUnitReader`、P3 `KnowledgeBuildRepository` 及 P4 `VaultFinalizeRepository` 的 Protocol。
- `defaults/config.json`：来源、阅读、检索分开的起始配置；每次读取返回独立对象。
- `examples/`：手工编写的中文/emoji、条款、表格链接及完整资产样例；两块正文和一个表格资产；12 个错误记录。

在仓库根目录验证：

```bash
PYTHONPATH=hermes-source-units/src python3 -m hermes_source_units knowledge_build hermes-source-units/examples/knowledge-build.json --units hermes-source-units/examples/units.json
python3 -m pytest tests/test_source_unit_contracts.py -q
```

以下 wheel 命令仅用于开发验证，不是 Hermes Skill 的部署要求：

```bash
python3 -m hermes_source_units unit /path/to/unit.json
python3 -m hermes_source_units knowledge_build /path/to/build.json --units /path/to/units.json
```

这些命令只读输入。成功返回 `source_integrity_verified: false` 和 `authorization_verified: false`，强调校验范围。错误返回结构化 code 和退出码 2；不会尝试修复文件或重建索引。

P2 的实际入口在完整 controlled-ingest Skill 内：

```bash
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" prepare-bundle --bundle "10_Raw/converted/example_bundle"
python3 "<ingest-skill-root>/scripts/manage_source_units.py" --vault "/path/to/vault" build --artifact-manifest "_system/sources/artifacts/<resource>/<revision>/manifest.json" --actor "<actor>" --expected-revision 0
```

artifact 按 resource/revision 保存规范正文、outline、资产和 manifest；UnitSet v2 按 resource/unit-set 保存 manifest、sections、`engine.json` 与 JSONL，并用 revision-checked `current.json` 选择当前版本。`engine.json` 钉住画像、策略尝试/拒绝、统计、token audit、coverage 和诊断。preview 不写 UnitSet，build 在完整校验后原子发布。具体命令和阶段门禁见 controlled-ingest 的 `references/source-units.md`。

P3 使用同一 Skill 内的入口；请求正文通过 JSON 文件传入，避免 shell 转义改变证据或页面内容：

```bash
python3 "<ingest-skill-root>/scripts/manage_knowledge_build.py" --vault "/path/to/vault" plan --request task.json
python3 "<ingest-skill-root>/scripts/manage_knowledge_build.py" --vault "/path/to/vault" pass --request pass.json
python3 "<ingest-skill-root>/scripts/manage_knowledge_build.py" --vault "/path/to/vault" reduce --request reduce.json
python3 "<ingest-skill-root>/scripts/manage_knowledge_build.py" --vault "/path/to/vault" finalize --request finalize.json
```

P4 由独立 Skill 执行，不让 ingest 隐式完成全库收尾：

```bash
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" "/path/to/vault" plan --request plan.json
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" "/path/to/vault" apply --request apply.json
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" "/path/to/vault" validate --release-id <release-id>
python3 "<finalize-skill-root>/scripts/manage_vault_finalize.py" "/path/to/vault" status
```

### 部署修正：保持直接复制 Skill 目录

用户的实际部署是将仓库内 Skill 目录直接复制到 Hermes skills 目录。Hermes 读取 SKILL.md 并执行其指定脚本；它不会自动发现仓库旁边的 Python distribution，也不会自动安装 wheel。因此撤销“用户需向 Hermes 环境额外安装同一 wheel”的部署前提。

P1/P2 的交付方式是：需要共享逻辑的 Skill 自带脚本、模块及 schema/defaults，脚本按自身位置加载，复制完整 Skill 目录即可使用；不要求全仓库 checkout、全局 PYTHONPATH、额外 pip install、独立虚拟环境或服务。P5 将需要的同源逻辑随 Provider 自身发行物携带；Provider 不引用 Hermes 的安装路径。

源码可以继续在本目录维护一份，发行生成所需 Skill/Provider 的内置副本，并检查版本和内容指纹。直接复制部署要求这些生成内容已在交付的 Skill 目录内，不能让用户在目标机器补做构建。禁止人工分别修改副本；“单份维护”不要求运行时所有进程必须访问同一个物理文件。

0.4.0 继续不依赖 jsonschema，使用标准库检查本项目固定 schema 词汇与现有业务约束，不实现通用 JSON Schema 引擎。新增未知关键字、远程引用、递归定义或未知 format 一律报 UNSUPPORTED_SCHEMA，不静默漏检。JSON Schema 文件仍是共享的字段定义，无需维护两份字段表。

隔离复制测试已通过：模块和资源复制到临时 Skill 布局，入口按自身位置加载，在 `python -I -S`（不加载 site-packages、忽略 PYTHONPATH）下运行。P1 已接入 bootstrap 和 lint 的真实 Skill 交付和入口。现有测试框架 pytest 与可选的打包工具 setuptools/wheel 只在开发端使用；jsonschema 仅可作额外开发对照，不是运行或测试集的必装项。

**当前共享模块已内置到 bootstrap、lint、controlled-ingest 和 knowledge-finalize 的 `lib/` 目录，并通过真实 Skill 的无第三方依赖复制运行验证。** wheel 测试只证明 Python 包资源能打包，不证明 Hermes 能发现它。此实现不引入独立服务或额外后台进程。

## 已确定的语义

### 内容与版本

单元身份是完整的 `vault_id + resource_id + artifact_revision + unit_set_id + unit_id` 结构对象。业务 document/version 通过单元记录和 manifest 关联。来源路径只负责定位，不决定业务身份。

`artifact_revision` 是规范化产物版本，`unit_set_id` 是该产物的某次确定切分规则版本。相同来源身份和输入可重复生成；重新 bootstrap 的另一个库不继承旧库 ID。更换 embedding 不修改这两者。新体系的历史引用继续钉住原版本，模糊 lineage 后置。

### 坐标与路径

- 文本为明确规则 `lf-codepoint/v1`：只将 CRLF/CR 统一为 LF，不暗中 trim 或改 Unicode 组合形式。
- `span` 为 **Unicode code point、0-based、半开区间**，且 `start < end`。
- 单元 `locator.span` 与消费引用 `source_ref.span` 都是该源文件上的**绝对字符范围**；引用 span 为 null 表示整个单元。
- `line_start/line_end` 为 1-based、两端包含，供展示。页码是已知的物理页序号、1-based，未知使用空数组，不推测 PDF 印刷页标签。
- 全资产 locator 只有 `whole-asset` 精度，引用 span 必须 null；不假造 bbox 或单元格坐标。
- 单元 locator、资产路径相对于被钉住的 artifact 根目录；artifact_manifest 参数、quality_refs 和知识输出路径相对于 Vault。均使用 POSIX 相对路径语法，拒绝绝对路径、反斜杠、冒号和 `..` 等。
- P0 做词法检查；P2 还必须在实际根目录解析、检查 symlink/越界并校验文件指纹。

### 正文、上下文与来源类型

SourceUnit 的 locator + content_sha256 描述核心。标题面包屑和追加阅读上下文不计入核心坐标/hash。正文 Unit 是知识构建与 RAG 共用的 canonical chunk，可按 source 配置保留有限 overlap；章节 owned ranges 仍是非重叠责任范围，工作覆盖按 locator 范围并集计算。

`content_type` 区分 normalized_source、source_asset、ocr_transcription、model_derived。后两者必须有 derived_from；存在这个字段也不等于模型产物可以当原始证据。P2/P3 还要验证派生链及访问限制。OCR 和模型描述作为明确的派生文本产物定位，不能拿图像 bbox 当正文字符范围。

### P0 中的标识计算

提供 `canonical_json` 与 `fingerprint`：UTF-8、按 key 排序、紧凑 JSON、只接受整数数字，不接受 float/NaN，不作隐式文本规范化；这是项目自己的 `canonical-json/v1`，不是 RFC 8785 实现。

P2.1 的确定性哈希分层为：

1. artifact_revision：artifact 记录去掉 `contract`、`identity`、`artifact_revision` 后的对象。
2. unit_set_id：`{artifact_revision, engine_version, engine_fingerprint, effective_config_fingerprint}`。
3. unit_id：`{identity, unit_set_id, locator, content_sha256}`。
4. `effective_config_fingerprint` 只计算 `source.chunking`；embedding 模型和索引渲染指纹只计算 retrieval/Provider 配置。

P2 生成器已采用以上分层依赖并验证确定性 ID；P5 对同一 Unit 的最终索引渲染、tokenizer/模型版本另算投影指纹。P0 样例不把手工 window_id 或 input_tokens 当作已测量的索引数据。

### 状态与证据

新工作类型包括 knowledge_build、entity_extraction、table_qa，分别使用明确 task_contract。后两个任务协议名称是预留标识，P0 不提供抽取/核验执行器。

账本状态：pending → running → completed；running 可进入 blocked/failed/skipped；blocked/failed 经显式 claim 重试并递增 attempt。已完成任务不直接重开，新来源或新工作规则形成新任务。P3 以 revision 做乐观并发检查并报告重叠任务；不同任务仍可有意识地消费同一 Unit。

已展示的 context 不算 inspection。完成任务的 targets 必须被 inspections 覆盖，没有未解决 deferred。零输出完成需要理由。needs-qa 需要具体 qa_note；受其支持的页面保留 draft。草稿状态本身不是权限控制。

`hermes-knowledge-build/v4` 是 P0 的静态组合契约；P3 执行路径使用 `hermes-knowledge-pass/v1`、`hermes-knowledge-page-revision/v1` 和 `hermes-knowledge-build-run/v1`。每个页面的支持范围等于所选 citation candidates 的支持并集，且候选证据已被检查。多来源、多版本比较可在同库 reading/build 记录中表达；一个普通 Provider 索引记录仍钉住一个 canonical Unit。

P3 的页面修订钉住 authored_sha256、支持 UnitRefs 和 review，并将提交状态、QA、业务资格和可见性分开。P3 不自动批准业务资格：新页面保持 `business_status: unassessed` 与 `visibility: draft`。P4 可以发布审计 release，但同样不批准业务资格，因此这些页面继续明确排除在索引资格之外。

P4 的 `plan` 钉住当前 identity registry、Build revisions、UnitSet current pointers、页面哈希和显式 source changes。旧贡献不再属于当前 UnitSet 时标记 `stale`；撤回标记 `withdrawn`。仍有当前支持的页面进入 `review_required`，没有当前支持的页面进入 `blocked`，两者都不会被自动删除。`apply` 在写入前重算完整计划，并拒绝死链、歧义链接、页面漂移和 revision 冲突。

页面移动不改变 page_id。Finalize 只有在旧路径内容仍匹配已提交 page revision 或既有 Hermes redirect 时才写重定向；不覆盖无关文件。版本化 navigation 与 release manifest 保留可审计快照，当前 navigation 只是最新投影。Release manifest 最后写入；缺少 manifest 表示操作没有完成，可以按同一 plan 幂等恢复。

## 配置：参数先可用，效果待测

| 组 | 初始值与含义 |
|---|---|
| source.chunking | auto；target 512 code points，max 1024，overlap 80；中英文分隔符；oversized preserve-and-report |
| token_budget | 默认 audit/max 1024/tokenizer fingerprint null；未注入 counter 时报告 unavailable，不伪造 token 结果 |
| reading | 最大 12000 code points；不等于任意模型的 token 上限 |
| retrieval | 索引渲染 target 800 / max 1024 tokens；命中后 context 预算 2400 tokens |
| tokenizer | 默认 null，表示尚未绑定 embedding tokenizer，P5 必须解析实际 id 和固定 revision 后才能索引 |

这些数值是起始配置，不是性能结论。source.max 是普通可拆文本的大小限制；无法安全拆开的结构会记录 `oversized-protected-structure`，保留来源而不截断。P5 另行组织不超模型上限的检索输入。

`source.overlap_codepoints` 属于 canonical chunk 身份，变化会形成新 UnitSet。实际检索输入的标题和其他附带文本也计入 max_tokens；P5 不再另加普通检索 overlap。`context_max_tokens` 用于命中后的上下文读取，不计为已完成工作。

## 接口与失败语义

P2 实现对象由调用方传入显式 vault_root。接口原型在 interfaces.py：

| 接口 | 输入 | 输出与承诺 |
|---|---|---|
| preview | artifact manifest、完整配置、actor、expected revision | 预计 manifest/units/diagnostics；不发布权威记录 |
| build | 同 preview | 相同生成结果，完整校验后发布；mode=published |
| get | unit_ref、可选绝对子范围、actor/purpose/registry revision | 核心文本或资产、内容指纹、QA、实际治理 revision；按当前权限读取 |
| context | 核心 refs、访问上下文、max codepoints | core/context 分开、omitted refs、truncated 及 reason |
| plan/claim/read/pass | UnitRefs、任务 revision、actor、阅读预算和显式判断 | 可重试工作账本、持久化阅读材料包、候选/引用 Pass；context 不冒充 target coverage |
| reduce | 当前任务 snapshots、citation candidates、身份判断和页面正文 | 稳定 subject/page ID、跨来源支持并集及 draft 页面修订 |
| finalize | build revision、每页内容 hash、parent hash 与 review | 校验完再提交页面、revision sidecar、identity registry 和任务终态；中断可幂等恢复 |
| release plan/apply/validate | completed builds、source changes、release state revision | 影响集合、贡献 disposition、导航/重定向、索引资格及最后提交的 release manifest |

P0 已实现错误包括 INVALID_SCHEMA、INVALID_RANGE、UNRESOLVED_REFERENCE、OUTSIDE_UNIT、UNIT_SET_MISMATCH、INCOMPLETE_COVERAGE、UNINSPECTED_SUPPORT、PROVENANCE_MISMATCH、QA_REQUIRES_DRAFT、INVALID_BUDGET 等，均为 `ContractError(code, path, message)`。

P2 运行时还会返回 SOURCE_UNAVAILABLE、SOURCE_CHANGED、ACCESS_DENIED、REVISION_CONFLICT 和 UNSUPPORTED_FORMAT；TOKENIZER_REQUIRED 留给 P5。超大结构采用诊断，不能为绕过预算伪造一段已验证正文。

## Provider 扩展边界

保持 coarse-recall/v1 的既有字段与 candidate-navigation-only 语义；每个新候选在 `source_units` 字段附带 `provider_extension` 记录（样例 provider-extension.json）。普通索引文档直接对应一个 canonical Unit；扩展包含完整引用、projection_fingerprint、定位精度和明确能力版本。

P5 同时更新生产者、CLI/HTTP 传输与消费者，删除 Provider 独立 Markdown 切片入口；缺此能力明确报错。当前 P2 没有修改 Provider 的 normalize_candidate，也没有声称旧适配器已能透传。

## 校验等级及限制

| 层级 | 当前情况 |
|---|---|
| JSON shape/枚举/必填/额外字段 | 已实现；标准库实现当前契约词汇及相对路径检查，不支持的规则显式拒绝 |
| 局部范围、参数、类型、QA、状态一致性 | 已实现 |
| 给定清单内的精确引用、子范围、覆盖、候选/页面依赖 | validate_references 已实现 |
| schema-only 外部验证器 | 必须自己注册 vault-relative-path；纯 schema 不验证哈希、状态转换或跨记录关系 |
| 原件/规范正文实际回读、总覆盖、资产真实存在 | P2 已实现；OCR/model-derived 的完整派生 DAG 留给相应 adapter |
| registry 身份/hash/processing 与读取资格 | P2 已实现来源治理；P3 已实现稳定知识身份、真实 review、幂等任务与 Build Finalize；P4 已实现增量 release |
| tokenizer adapter 与只读计数 | P2.1 已提供 TokenCounter/audit 接口；真实 embedding tokenizer 和检索效果属于 P5 |

P0 的通过只表示契约实践通过，不代表已经完成 bootstrap、知识生成、检索或正式新库验收。

### P1/P2 distribution and configuration

Run `python3 tools/sync_skill_runtime.py` from this package directory after a
canonical source change, then run it with `--check`. Generated copies are committed
inside bootstrap/lint/controlled-ingest/knowledge-finalize; recipients only copy the complete Skill. Hashes describe
UTF-8 text with normalized LF line endings to support Windows/Linux checkouts.
Provider packaging remains P5 work. Runtime version is 0.4.0; existing record
contracts stay versioned independently, and the Vault declaration remains
`hermes-source-unit-vault/v1`.

P2.1 writes the combined `hermes-source-unit-config/v2` JSON rather than independent
YAML copies of the same budgets. `source`, `reading`, and `retrieval` are loaded and
validated together. Explicit complete bootstrap configuration wins over packaged
defaults; subsequent reads use the Vault copy and verify its fingerprint. Updating
configurations requires a future controlled update operation, not silent edits.
