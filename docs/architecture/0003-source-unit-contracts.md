# ADR-0003：来源单元共享契约与 P0 实践

2026-09-11。状态：P0 契约实践已实现；P1–P6 尚未实施。关联：[全新建库计划](../SOURCE_UNITS_EVOLUTION_PLAN.md)、[共享包与接口规范](../../hermes-source-units/README.md)。

## 范围

按本轮用户决定，完成新体系后重新 bootstrap 并从原件 ingest，不导入旧 ledger、旧 Provider chunk、旧页面引用或索引。已有治理原则和可靠代码能力复用，旧数据格式不要求兼容。本次不清空任何 Vault，不部署或切换线上流程。

## 复用与新增

继续复用 Bundle v2 的规范正文、outline、资产和解析质量；document/version/resource registry；knowledge-build v3 的候选、审阅和治理原则；coarse-recall/v1 的候选导航边界。新内容层先生成 Section/SourceUnit，ledger 记录其工作消费，Provider 构造检索窗口。当前运行代码尚未按此关系重构。

P0 新增开发态 Python distribution `hermes-source-units`，import 名为 `hermes_source_units`。按用户后续明确的复制式部署方式，撤销向 Hermes 环境额外安装 wheel 的前提。P1 将必需模块与资源内置到需要它们的 Skill 目录，入口按脚本自身位置加载；P4 随 Provider 自身发行需要的同源逻辑，不引用 Hermes 目录。开发源码单份维护，交付副本生成并校验指纹，不人工分别维护。

共享库 0.1.1 已消除 jsonschema 运行依赖，改为标准库的固定契约校验。未知 schema 特性拒绝执行；不另造通用 JSON Schema 引擎。复制模块到临时 Skill 布局后的 `python -I -S` 隔离验证已通过，P1 继续负责实际 Skill 入口与内置交付。不能因这项模块测试通过就声称实际 Hermes 部署已经更新。

## 已确定决策

1. 来源单元以规范产物的可验证范围/资产定位，正文留在版本化 artifact；不会复制成每块一个 Markdown。
2. section 内容归属由来源层计算；工作 ledger 不定义切片。结构父子、阅读上下文、检索映射分别表达。
3. source_unit 核心无重叠；阅读/检索窗口可组合与引用子范围，不形成第二套来源权威。
4. 文本坐标为 LF Unicode code point、0-based 半开区间。消费 span 与核心 span 同属源文件绝对坐标；null 表示全单元。上下文不计入核心哈希。
5. 完整 unit_ref 钉住库、资源、解析版本、单元集与单元 ID。不根据相似文本静默跨版本重定向；细粒度 lineage 后置。
6. 共享 schema 定义结构，Python 校验器补局部/引用一致性。文件真实回读、治理/QA、并发状态机和模型预算检查由后续实际运行模块负责。
7. 新知识构建记录固定为 hermes-knowledge-build/v4；P0 独立包验证该格式，现有 v3 运行入口不变，P3 才接入并保持业务约束。
8. 新 Provider 候选使用 source_units 扩展，不修改 coarse-recall/v1 的权威语义。P4 同步更新生产和消费；不依靠旧 fallback。
9. 初始数值是可调配置；embedding tokenizer 尚未配置时明确 null，不假装可以直接索引。

## P0 实际交付

| 子项 | 落地 |
|---|---|
| P0.1 | 本 ADR：复用与新增、共享包部署、边界和首版范围 |
| P0.2 | contracts.json：artifact、section、unit、unit-set、引用、窗口、任务、knowledge-build 和 Provider 扩展 |
| P0.3 | interfaces.py 的 build/preview/get/context 原型；默认配置；只读校验 CLI 与错误语义 |
| P0.4 | 中文/emoji 正文与表格资产、完整手工样例、12 个错误样例、契约/覆盖/依赖测试 |

首版 P2 格式为工程 PDF（经既有 MinerU/Bundle adapter）与普通 Markdown。P5 前按正式原件清单补齐需要的 Office/独立图片 adapter；这不是默认跳过材料的许可。本次手工样例不是生产解析结果，且未触发 LLM。

## 实践结论与下一步

测试覆盖语义上的高风险边界：混版本引用、遗漏多来源支持、未读证据、把部分阅读标成完整、QA 降级丢失、越界路径/坐标和错误预算。打包验证确保 schema/defaults 随 wheel 发布，运行环境不需要源码 checkout 才能读取契约。

这使 P0 有可运行产物，而非继续停留在字段表。下一阶段是 P1 的新 bootstrap 骨架与实际配置消费，随后 P2 实现当前仅由 Protocol 声明的生成/读取服务。具体执行及测试结果见 [P0 验收记录](../SOURCE_UNITS_P0_ACCEPTANCE.md)。
