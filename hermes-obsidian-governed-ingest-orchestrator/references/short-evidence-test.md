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
