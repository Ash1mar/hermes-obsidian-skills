# 主题与原文证据映射实验

使用独立的离线入口核算测试材料。输入是显式选择的 Vault 和来源清单。清单每行提供
`original`、`resource_id`、`unit_set_id` 和 `bundle_manifest`。路径相对于 Vault。
程序用当前注册表、原件 SHA、转换 Bundle、已登记的 UnitSet 和完整资产验证输入。
请求清单含原文和资产，保存在 Vault 外的受控工作目录。

```bash
python3 "<skill-dir>/scripts/prepare_short_evidence_test.py" \
  --vault <vault> --sources <source-pins.json> --output <offline-plan.json>
```

预览列出可渲染的主题提取请求、预留主题目录空间的映射模板、逐单元覆盖和
40,000／50,000 字符的章节装载样本。真实主题目录进入映射请求后再测量完整输入。
出现超限时按连续单元重新装包。单个完整单元超限时保留阻塞证据。
输出估算用于分包，实际 tokenizer 和模型费用由后续观测提供。

映射记录用短编号标识主题、原文、条件、例外、定义、上下文、相反证据、冲突和必读资产。
每个主分配单元都带关联、排除或待处理状态。排除与待处理都有理由。来源定位、哈希和 QA
由程序绑定。对明确待处理项最多准备一次局部映射请求。程序合并重复关系，直接汇总主题
供页面协调。协调只为明确争议提供相关完整原文，随后最多按问题准备一次局部协调。

写页展开整块原文、所列依赖、QA 和完整资产。已确定的独立章节可以分请求生成并在同一
逻辑页汇合。具备多主题边界且完整材料适合局部规划预算时，超限页最多准备一次本页细化
请求。其余超限页保留带原因的待细化项。每个写页请求最多包含两个独立槽位。
程序验收检查逐槽引用、必读资产、章节齐全和 QA 状态，记录独立模型审查状态为
`not_performed`。实验产物的发布资格固定为 `false`。

传入 `--receipts <目录>` 可读取实验回执并生成当前阶段的待办。要离线检查已保存的响应，
同时提供 `--plan <当前计划>`、`--request-id <ID>` 和 `--response <JSON>`。
阶段请求由现有来源和前序回执重新构建，旧输入、修改后的回执或错误编号会被拒绝。
双槽响应逐槽保存，恢复时优先验证已经保存的槽。待办中的 `missing_slots` 用于识别
应补的响应槽。此入口只读来源，所有实验结果写入指定的 Vault 外位置。

用户明确授权后，用以下入口从一个已结束的历史测试 workflow 复用其准确的 post-MinerU
来源边界。实验使用独立原生测试板，写页按 40,000 字符装包。每个请求绑定单个原生
工作任务及持久化请求，收到但未通过结构检查的结果最多追加一次受限请求。未知消耗保持
待处理。实验结果记录阶段、原生回执、token、重试和覆盖缺口，发布资格保持关闭。

```bash
python3 "<skill-dir>/scripts/run_short_evidence_test.py" start \
  --vault <origin-vault> --origin-workflow <workflow-id> \
  --sources <source-pins.json> --operation-id <stable-id> --output <experiment-dir>
```

实验停止并排空原生租约后，复用同一个目录和原操作 ID。使用新的稳定重进 ID 绑定
本次范围。先加 `--dry-run` 查看上游回执、当前阶段、未完成请求及旧调用保留状态。
支持 `mapping`、`coordination` 和 `writing`。已验证结果会被复用。
已预留请求的原生响应先按会话、输入和运行身份回放。未知消耗与失败的验证继续保留
为缺口。新的调用只派发尚未预留的请求。

```bash
python3 "<skill-dir>/scripts/run_short_evidence_test.py" reenter \
  --vault <origin-vault> --origin-workflow <workflow-id> --sources <source-pins.json> \
  --operation-id <stable-id> --output <experiment-dir> \
  --from-node mapping --reentry-id <stable-reentry-id> --dry-run

python3 "<skill-dir>/scripts/run_short_evidence_test.py" stop \
  --output <experiment-dir> --reentry-id <stable-reentry-id>
```

新实验固定 `short-evidence-pages/v2`。已停止的旧实验可从协调节点显式选择该合同。
使用 `reenter --from-node coordination --page-contract short-evidence-pages/v2`，其余参数沿用
原实验。入口复用原映射回执，旧协调与写页响应仍由原合同解释。后续重进沿用当前合同。

协调要求每页提供读者问题、适用范围和归组理由。目录附带每主题完整材料与资产体量。
结构完整性由程序检查，合并语义仍由协调负责。明显过宽的分组只安排一次局部协调。
写页沿原章节、主题和可分的原文单元形成片段。锚定条件、例外和整张表跟随片段，适用
范围未细分的上下文可重复。原子依赖超限时记录具体材料，其他片段仍可独立生成。
组装要求全部预期片段验证成功。

新原生操作固定 `short-page-layout/v2`。现有协调请求为每个完整计划页返回
`page_type`：`card`、`concept`、`entity`、`topic` 或 `project`。沿用现有
主题归属和写作分片；不从片段标题推断页面类型。已确认可单独交付的补充章节仍为 Card。
路径由程序按类型生成，见受控摄取的 [Vault 目录约定](../../hermes-obsidian-controlled-ingest/references/vault-structure.md#typed-short-path-pages)。

Project 的 `project_basis` 包含原文中的项目名称和该页所属主题内一至三个文本编号。
程序检查名称出现在绑定原文，且证据随片段进入正文并被引用；未通过时只隔离该页和
实际依赖项。Topic 不要求项目依据。项目语义和引用支持仍需从真实产物判断。
类型进入 Build 版本、现有身份注册表和 Markdown 属性。旧 `kind: concept`
是短路径身份命名的一部分，不表示页面都是 Concept。既有操作继续使用原来的路径、
提示词与回执；新布局不迁移旧页，遇到已注册身份也不覆盖正文或自动改路径。
发布仍使用现有的逐页资格、授权、证据和覆盖检查；Provider 读取发布清单中的实际路径。

新知识页规划在同一个协调响应中区分 `pages` 和 `syntheses`。每个映射主题仍由
一个主要计划页或一个待解决项负责。可以脱离专题理解、且必要条件和例外能完整保留的
条目单独规划；不能脱离上下文的内容继续在一个计划页内按写作预算拆片。
专题可以围绕至少两个独立 Card、Concept 或 Entity 的一个具体关系另建页，选取这些
条目已映射的原文锚点，不改变条目的覆盖归属。专题写作按既有关联带上适用条件、例外、
定义和必读资产，对综合结论直接引用原文，不复制子页正文或假设子页已经发布。
缺少合格锚点、必要证据未解决或完整证据包超限时，只隔离该专题及实际依赖项。

新操作的规划记录提供已登记身份、路径、版本和类型。相同注册身份停在原页的修订边界；
同名仅作为候选提示，不能据此判断同范围、同来源版本或覆盖原正文。专题关联留在规划
和 Build 证据中供导航使用。已经存在的操作继续按其钉住的规划契约重进。

新导航契约在发布完成后从已登记且在当前发布中合格的页面生成
`90_Dataview/短路径知识导航.md`，按类型、主题、来源和实际项目组织。专题关系单独在
索引中展示，只链接已发布且合格的条目；项目可以显示正式发布已有的关联链接。
可选导航缺口记录为诊断，不影响独立合格页的交付；正文中确实存在的依赖仍按原规则校验。
导航不改正文、发布清单或 Provider 索引，不发起模型调用。手工文件、有改动的生成索引
及无内容校验标记的旧目录索引均保留。

单独维护总索引时，先用当前发布 ID 预览。仅在明确授权写导航时添加 `--apply`：

```bash
python3 "<skill-dir>/scripts/refresh_short_navigation.py" \
  --vault <vault> --release-id <current-release-id>
```

重进历史操作仍按原契约；预览和刷新入口不发布页面，也不改变原有暂停或查询权限。

片段回执分别记录正文小节内的证据、上下文、未解决项和资产查验。经程序确认的纯导航
包装块可以由其完整表格承担引用。引用诊断包含具体编号，原始响应保持可复核。
来源 QA 原样留存，另按信息性、影响待定位和已定位材料故障记录写作与发布影响。
待定位 QA 保留发布审阅项，显式局部故障阻断其依赖片段。实验发布资格保持关闭。

停止后，`reenter --from-node writing --fragment-repair <JSON>` 可选择原写页计划中的
失败片段。清单固定 `short-fragment-repair/v1`、`expected_pages_sha256` 和
`changes`。每项固定片段 ID、旧回执哈希或空值、处置及同页覆盖候选。
`scope` 按原片段主题生成单槽职责及资产绑定，最多新调用一次。`equivalent` 先验证
同页已验收正文的原文身份、关系、引用、整表查验及 QA，覆盖等价则只记录覆盖证明。
`isolate` 保留原已消耗请求及实质缺口。其他片段沿用原回执，覆盖证明不重复组装正文。

`--fragment-exclusions <JSON>` 固定已确认的内容错误、片段回执和受影响页。清单采用
`short-fragment-exclusions/v1`，含 `evidence` 的相对引用及哈希和 `excluded` 列表。
组装和覆盖复用核对该清单，旧响应和消耗保持可审计。只有被排除且能唯一定位续文的
原片段可以用 `context` 处置重进。新请求固定同版原文的准确位置和其他页的处置状态。
结构化缺料报告分别标记随包提供、位于其他页、登记存在但未随包提供、无法定位。
未定位的关系保留缺口。后续重进沿用当前的排除和修复范围。

正式接入需要单独授权精确候选。选择最新的已结算写页入口，并提交
`short-formal-authorization/v1` JSON，包含 `writing_entry`、`checkpoint`
（控制、结果和最终组包的 SHA256）、来源工作流 `actor`、已排序的 `pages`、
当前 `base_release`、布尔值 `publish` 及现有内部检索 `policy`。

```bash
python3 "<skill-dir>/scripts/run_short_evidence_test.py" formal \
  --output <experiment-dir> --reentry-id <formal-stable-id> \
  --formal-authorization <authorization.json> --dry-run
```

正式执行去掉 `--dry-run`。专用原生程序任务验证已结束的写页运行、精确请求、
本地会话响应和回执，并在原 Vault 通过正式 Build 提交页面版本。记录使用独立的
`native-short-build/v1` 凭据。原始 QA 作为标注随版本保留。原生程序按页复核注册原文、
已验收片段、必读资产和覆盖证明，记录有具体依赖的材料故障和未定位标注。专业 QA
保持未执行，未定位标注本身不阻断发布。部分发布继续检查来源资格、正文依赖和隔离。
对已完成的短 Build，向同一入口追加 `--existing-build <run-id>` 并使用新的稳定重进 ID，
复用原授权、正文和 Build 回执，追加可审计的逐页标注记录后执行部分发布。
无合格页时保留原发布标识。此入口没有模型调用。
