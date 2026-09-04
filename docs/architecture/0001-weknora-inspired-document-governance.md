# ADR-0001：借鉴 WeKnora 的最小文档治理与版本模型

## 状态

已接受，阶段 1 和阶段 2 已实现。

本 ADR 于 2026-09-04 确认阶段 1 至阶段 5.5 的目标架构。阶段 1 已为新建 engineering Vault
加入 JSON 治理控制面及只读 Lint；阶段 2 已实现统一文档治理管理器及 JSON repository。旧 Vault、
Bundle、Query 和 Provider 行为保持兼容。后续字段只有在相应脚本、测试和兼容读取完成后才成为
运行时合同。

## 背景

现有系统已经具备受控原件保存、PDF/Image Bundle v2、section ledger、分层查询、Query trace、
证据门禁和可替换 coarse-recall Provider。随着材料扩展到多个来源机构、系统、项目和版次，当前
以文件、Bundle 和 section 为中心的身份模型不足以回答以下问题：

- 两个机构提供的相同文件是重复内容，还是两个需要保留的来源事件；
- 同名但内容不同的文件是升版、独立文档，还是待确认冲突；
- 哪个版本是当前有效版本，旧版是否仍可用于历史查询；
- 原始目录、业务分类、访问边界和物理存储位置之间是什么关系；
- 文件从本地迁移到 OSS 后，既有证据引用和版本关系是否仍然稳定。

2026-08-05 的原计划试图同时建设完整治理包、批次状态机、Source Bundle v3、多格式转换、
Vault 布局间接层和 Vault Transform。该范围过大，也会重复建设通用知识平台已经验证的能力。

WeKnora 的公开实现提供了可借鉴的工程分层：Tenant/Workspace 是隔离边界，KnowledgeBase 是
知识容器，Knowledge 是一条文档或内容记录，Chunk 是检索单元；`FolderPath` 仅用于浏览导航，
不决定实际 `FilePath`；文件服务通过统一接口屏蔽 local、MinIO、OSS、S3 等存储后端。其普通
Knowledge 模型和文件哈希去重适合一次上传及重新解析，但不直接表达本项目所需的业务文档
版本谱系。

参考：

- [WeKnora 核心概念](https://github.com/Tencent/WeKnora/blob/main/website-docs/01-getting-started/01-introduction.md)
- [WeKnora Knowledge 数据结构](https://github.com/Tencent/WeKnora/blob/main/internal/types/knowledge.go)
- [WeKnora 文档入库与文件存储](https://github.com/Tencent/WeKnora/blob/main/website-docs/02-architecture/03-document-pipeline.md)
- [项目官方技术规范](../OFFICIAL_TECHNICAL_SPECIFICATION.md)

## 决策驱动因素

1. 先解决错误版本、未知权威性和跨机构重复来源造成的检索正确性风险。
2. 复用现有 Vault 控制面、Bundle、ledger 和 Provider，不进行平台级重写。
3. 保留未来接入 OSS、数据库或 WeKnora 的能力，但不提前实现这些基础设施。
4. 让文档版本成为后续本体抽取、事实失效传播和 Wiki 重编译的稳定依据。
5. 保持旧 Vault 和 Bundle v2 可继续使用，避免一次性迁移现有材料。

## 决策

### 1. 采用 WeKnora 式分层，但不复制平台实现

概念映射如下：

| WeKnora | 本项目 | 约束 |
| --- | --- | --- |
| Tenant / Workspace | Vault / `security_domain` | 硬隔离边界 |
| KnowledgeBase | `collection_id` | 检索和业务分组，不自动成为安全边界 |
| Knowledge | Document Version | 一份不可变内容版本及其治理状态 |
| Chunk | Bundle section | 继续使用现有 outline、ledger 和 query-index |
| FolderPath | `folder_path` / `original_relative_path` | 仅用于浏览和来源审计 |
| FilePath / resource catalog | `resource_id` + `storage_uri` | 稳定身份与物理位置分离 |
| ParseStatus | `processing_status` | 仅描述处理管线，不代表业务有效性 |
| CustomMetadata | governance metadata | 保存来源、版次、权威性和状态 |

第一阶段使用 Vault 内 JSON 控制面，不引入 WeKnora 的数据库、Web UI、租户成员、API Key、任务队列
或存储管理服务。JSON 是 `hermes-governance/v1` 的首个存储适配器，不是要求上层长期直接操作文件的
业务接口。

### 2. Vault 是硬安全边界

- 一个 Vault 只能属于一个 `security_domain`。
- `collection_id`、来源机构和目录都不是硬权限边界。
- 需要不同人员、网络区域、保密等级、审计责任或保留策略时，必须拆分 Vault。
- MVP 不实现同一 Vault 内逐文档 RBAC，也不实现跨 Vault 在线共享。
- 未来即使引入服务端权限，Vault 控制面仍必须记录其安全域，不得依赖外部服务猜测。

### 3. 来源机构与访问组织分离

`source_organization_id` 表示材料的提供者或发布者，只用于溯源、筛选和治理，不授予或限制访问。
它不得复用为用户组织、权限组或 WeKnora Organization 的等价物。

MVP 使用一个受控的 `source-organizations.json`。名称和别名可以调整，稳定 ID 不得因显示名称变化
而改变。无法确认的机构进入待确认状态，LLM 可以提出映射候选，但不得创建或批准正式机构 ID。

### 4. 文档身份采用三层模型

```text
document_id   同一逻辑业务文档跨版本稳定
version_id    某个不可变内容版本
resource_id   原件的稳定存储引用
```

约束如下：

- `document_id` 不得由当前文件路径或文件名直接充当。
- `version_id` 对应确定的内容 SHA-256；完成登记后不得原地替换内容。
- `resource_id` 与 `storage_uri` 分离。迁移物理存储只能改变资源映射，不改变文档或版本 ID。
- 同一 `document_id` 在一个 Vault 的 MVP 中最多有一个 `active` 版本。
- `supersedes_version_id` 必须指向同一 `document_id` 的已有版本，并且版本关系不得成环。
- 文件名、目录名、修改时间或模型推断不得单独触发自动升版。
- 无法确定逻辑身份或版次关系时，登记为候选并等待确认，不得静默覆盖。

若未来确认同一逻辑文档需要同时存在多个适用范围不同的现行版本，必须先扩展 schema 和查询语义；
在此之前应使用不同 `document_id`，不得放宽“最多一个 active”约束。

### 5. 相同内容与来源事件分离

相同 SHA-256 可以复用同一个 `version_id`、转换结果和检索内容，但每次独立提供仍必须保留来源
事件。MVP 允许在文档版本记录中保存 `source_occurrences` 数组，每项至少包含：

```text
source_occurrence_id
source_organization_id
source_collection_id
batch_id（可选）
original_relative_path
received_at
```

阶段 1 不单独建设来源事件数据库或完整批次管理器。当来源事件数量、并发更新或审计量证明内嵌
数组不足时，再通过兼容迁移拆分独立 registry。

### 6. 物理目录与逻辑分类分离

- `original_relative_path` 保留材料提交时的相对路径，用于审计。
- `folder_path` 用于 Vault 或未来 UI 中的浏览导航。
- `collection_id`、系统、项目、文档类型和机构使用独立元数据表达。
- 目录名只能产生候选分类，不得成为权威分类或权限判断的唯一输入。
- 现有 `10_Raw/` 不在阶段 0 至阶段 4 中批量迁移或重命名。

新试点可以使用以下结构，但该结构在试点验收前不进入 Bootstrap 默认输出：

```text
10_Raw/source-collections/<source_collection_id>/payload/<original-relative-path>
```

### 7. 处理状态与业务状态分离

`processing_status` 借鉴 WeKnora 的处理状态机，只描述技术处理：

```text
pending -> processing -> completed | failed
```

`governance_status` 描述业务版本是否可默认使用：

```text
candidate | active | superseded | withdrawn | unknown
```

`authority_status` 描述材料权威性质：

```text
official | reference | draft | unofficial | unknown
```

`processing_status: completed` 不得推导出 `governance_status: active` 或
`authority_status: official`。激活新版本时，旧 active 版本与新 candidate 版本必须在一次原子更新
中分别变为 `superseded` 和 `active`。

### 8. 默认检索执行版本门禁

启用治理模式后，普通查询和默认索引语料只允许：

```text
processing_status = completed
governance_status = active
security_domain = 当前 Vault
```

显式历史、版本比较、作废材料审查等问题可以纳入 `superseded` 或 `withdrawn`。Provider 过滤不能
替代 Query 最终门禁；Vault 注册表是版本状态的权威来源，Provider 索引仍是可重建数据面。

未启用治理模式的旧 Vault 继续执行当前检索合同。不得因为缺少新注册表而自动把旧材料补记为
active 或 official。

### 9. 存储先保留接口，不实现多后端

MVP 继续使用本地文件，但从第一天记录：

```text
resource_id
storage_uri = local://...
```

后续可以增加 `oss://`、`s3://` 或服务端资源目录。凭据、绝对主机缓存路径和临时访问令牌不得写入
Vault。阶段 0 至阶段 4 不实现多后端管理、迁移服务或临时授权 URL。

### 10. 保持现有合同兼容

- Bundle 保持 `2.0`，后续只允许增加可选的 `governance` 字段，暂不建设 Source Bundle v3。
- Section ledger、query-index、query trace 和 `hermes-coarse-recall/v1` 不在本决策中升级版本。
- 没有 `_system/vault.json` 的 Vault 继续采用现有兼容路径，Lint 将其报告为 `legacy`，不据此判错。
- Bootstrap 仅在显式选择 `engineering` profile 时创建治理文件，且即使传入 `--force-empty` 也不得
  覆盖或升级已有治理控制面。
- 后续实现必须先进入 `main`，通过测试后再按分支维护合同 merge 到 `intranet`。

### 11. 先固定持久化合同，后替换数据库适配器

上层逻辑采用以下依赖方向：

```text
Bootstrap / Ingest / Query / Lint
             -> Governance Service
             -> Governance Repository (`hermes-governance/v1`)
                    -> JSON（当前）
                    -> SQLite / PostgreSQL（阶段 5.5）
```

阶段 1 的 `document-governance.schema.json` 固定稳定 ID、状态词表、来源事件、版本关系和关系表映射。
`_system/vault.json` 显式声明 `repository.backend: json`，避免调用方靠文件存在性猜测后端。阶段 2
开始，写操作必须收口到文档治理管理器；Ingest 不得绕过它手工改写注册表。JSON 更新必须使用校验、
原子替换和单调递增的 `registry_revision`。

迁移数据库时保持服务与 repository 合同不变，先执行可复核的 JSON export/import，再切换唯一权威
后端。禁止 JSON 与 SQL 长期双写；切换后 JSON 只能作为带 revision 的审计快照或导出物。SQLite 用于
单机试点，PostgreSQL 用于需要多进程写入、事务与服务化部署的环境。数据库、迁移工具和连接凭据均
留在运行主机，不进入 Vault。

## 最小后续实现范围

本 ADR 批准以下后续增量，具体实现仍需分别评审和验收：

1. 阶段 1（已实现）：engineering Bootstrap 创建 `_system/vault.json`、单一 governance schema、
   来源机构表和 JSON 文档注册表；Lint 校验身份、来源、状态和版本关系，旧 Vault 保持兼容。
2. 阶段 2（已实现）：Controlled Ingest 内的治理管理器支持 `validate/register/activate/status/add-source`
   以及来源机构登记/审批；所有写入执行 revision 检查、互斥锁、全状态校验、审计事件和原子替换。
3. 阶段 3：Controlled Ingest 登记、处理状态更新和 Bundle v2 可选治理投影。
4. 阶段 4：Provider 语料过滤、Query 二次门禁和 Vault Lint 检查。
5. 阶段 5：20 至 30 份多机构、多版本材料的独立试点。
6. 阶段 5.5：实现 SQLite repository 与迁移工具；仅在出现并发或服务化需求时增加 PostgreSQL，
   并用同一合同测试验证两个后端。

阶段 5 通过后，下一项核心研发应是本体约束提取以及事实、版本、证据和 Wiki 之间的可追溯关系。

## 明确延后或排除

以下内容不属于阶段 0 至阶段 4：

- 自建用户、租户、角色、API Key 和逐文档 RBAC；
- OSS、MinIO、S3 的真实适配、凭据管理和存储迁移；
- 完整 ingest batch 状态机和定时同步；
- Source Bundle v3；
- DOCX、PPTX、XLSX 全格式原生转换；
- MinerU review patch engine；
- Vault Transform、拆分、合并和 `_system` layout v3；
- Web 文档管理后台；
- 自动批准文档身份、业务版次、权威状态或机构映射；
- 自动 Wiki/GraphRAG 发布。

SQLite/PostgreSQL 的实际连接、ORM、迁移脚本和数据库运维延后到阶段 5.5；阶段 1 只交付数据合同、
后端声明和可迁移关系映射。

## 被否决或暂缓的替代方案

### 一个来源机构一个 Vault

默认否决。机构不是天然安全边界，该方案会割裂跨机构检索、重复概念和索引。只有实际权限、网络、
责任或法规边界不同才拆 Vault。

### 所有文件平铺且只依赖文件夹或文件名

否决。无法可靠表达来源、版本、权威性、适用范围和重复提交。

### 完整复制 WeKnora

否决。其平台能力会扩大当前维护面，并且普通 Knowledge 上传模型不能替代本项目所需的业务版本
谱系。WeKnora 可作为未来适配目标或实现参考，不是当前运行依赖。

### 一次性实施 2026-08-05 完整计划

暂缓。治理包、批次系统、Bundle v3、多格式转换、存储服务和 Vault Transform 必须由试点中的
实际需求逐项触发，不能在版本内核验证前并行建设。

### 数据库立即成为运行时权威

暂缓。当前单机和小团队规模先使用可审计的 JSON repository；数据库兼容性现在就进入 schema 和
repository 合同，但实际后端在原有阶段完成并经过试点后实现。这样能避免无消费者的空数据库、
JSON/SQL 双写和过早运维负担，同时避免未来重新设计身份与关系模型。

## 验收门槛

阶段 0 的验收条件为：

1. 文档明确区分已决定事项、后续实现和非目标。
2. 与官方技术规范的来源/派生、控制面/数据面、Ingest 写入和 Query 只读边界一致。
3. 不修改现有运行合同、schema 版本、部署配置或 Vault 内容。
4. 文档索引能够发现本 ADR。
5. 工作树只包含预期的文档变更。

后续阶段 1 至阶段 4 至少必须证明：

- 一个逻辑文档不会出现两个默认 active 版本；
- 相同内容由两个机构提供时只建立一份检索内容，并保留两个来源事件；
- 新内容不会仅因同名而覆盖旧内容或自动升级为现行版本；
- 普通查询排除旧版和作废版，显式历史查询仍可定位它们；
- 物理存储位置变化不改变文档、版本和证据身份；
- 旧 Vault 在未启用治理模式时无行为回归。

阶段 1 验收范围覆盖治理文件生成、覆盖保护、schema/数据库映射以及 Lint 对关键不变量的只读检查。
阶段 2 覆盖人工或受控调用的登记与状态事务，但尚未由 Bundle 转换流程自动调用；Ingest 自动联动、
Query 门禁和 Provider 过滤分别属于阶段 3 至阶段 4，不能因管理命令已存在而宣称已经完成。

## 结果

该决策把近期工作从“建设一个缩小版通用知识平台”收缩为“在现有 Skill 和 Vault 上增加可移植的
文档治理内核”。它复用 WeKnora 已验证的隔离、容器、文档、目录和存储分层，同时把有限研发
资源集中到业务版本谱系、来源审计和默认检索安全，为后续本体与 Wiki 编译提供稳定输入。
