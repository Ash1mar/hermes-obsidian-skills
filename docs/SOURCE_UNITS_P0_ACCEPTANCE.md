# 来源单元 P0 实践验收

日期：2026-09-11。结论：**P0.1–P0.4 的本地契约实践完成，可进入 P1。** 这不是 P2 切片器或新库端到端验收。

部署前提更正：用户直接复制 Skill 目录到 Hermes，因此原“另行安装共享 wheel”的方案撤销。0.1.1 已实现零运行时第三方依赖及隔离复制模块测试；实际 Skill 内置与入口接线仍由 P1 完成，不能将模块验证等同实际 Hermes 部署验收。

## 交付核对

| 项目 | 实际交付 | 验收情况 |
|---|---|---|
| P0.1 复用与决策 | [ADR-0003](architecture/0003-source-unit-contracts.md) | 明确已有 Bundle/治理/构建基础、共享包位置和全新建库边界 |
| P0.2 类型与规则 | [contracts.json](../hermes-source-units/src/hermes_source_units/schemas/contracts.json)、[校验器](../hermes-source-units/src/hermes_source_units/validation.py) | 必填、枚举、范围、类型及跨记录引用规则可运行 |
| P0.3 接口与配置 | [接口原型](../hermes-source-units/src/hermes_source_units/interfaces.py)、[默认配置](../hermes-source-units/src/hermes_source_units/defaults/config.json)、[使用与错误语义](../hermes-source-units/README.md) | build/preview/get/context 明确；只读校验 CLI 可运行 |
| P0.4 正反样例 | [正文](../hermes-source-units/examples/source/document.md)、[单元](../hermes-source-units/examples/units.json)、[错误样例目录清单](../hermes-source-units/examples/invalid/cases.json)、[测试](../tests/test_source_unit_contracts.py) | 真实样例字符范围/hash/覆盖与引用关系经过校验，12 类错误样例按预期拒绝 |

## 0.1.0 初次实践记录（历史）

- `python -m pytest tests/test_source_unit_contracts.py -q`：**43 passed**。
- `python -m pytest tests -q`：**165 passed**，包含新增 43 项，未破坏现有仓库测试。
- 使用本地 setuptools 84.0.0 / wheel 0.48.0 构建 `hermes_source_units-0.1.0-py3-none-any.whl` 成功。
- 另用 Python 3.11 从 wheel 本身导入包，读取已打包的 schema/defaults，并校验配置与知识构建引用成功；不通过源码目录补取资源。
- 构建 wheel SHA-256：`a71795b4a3af0a48cd4320c18a49a7f40043fac185a3aa74133e4cfd9d0b6f31`。这是本次本地产物指纹，不承诺不同时间重打 wheel 字节相同。
- 校验运行使用 jsonschema 4.26.0。无模型调用，无外网依赖下载，无 WSL/生产部署。

中间校验发现并修正：分隔符规则不能拒绝合法的纯换行；错误样例制作必须消除 Python 共享对象引用，避免修改页面来源时连候选来源一起变化而掩盖遗漏来源测试。最终全部用例通过。

## 0.1.1 轻量化修订

移除 jsonschema 及其间接依赖的运行要求。project.dependencies 为空，核心全部使用 Python 标准库；保留 schema 文件作为单一字段定义，固定词汇校验器对未知特性显式报错。

- 契约测试 **56 项通过**，新增严格数值类型、不支持的 schema 特性和复制运行测试。
- 仓库测试合计 **178 项通过**，包含上述 56 项。
- 0.1.1 wheel 构建成功；在 `python -I -S` 下直接从 wheel 导入、读取默认配置并校验成功，METADATA 不含 Requires-Dist。
- 隔离测试复制模块及资源到临时 Skill scripts/lib 目录，使用 `python -I -S`，断言 jsonschema 不可导入，并检查正反例 CLI 返回值。无需源码仓库路径或全局 PYTHONPATH。
- 在现有开发环境中，以 jsonschema 对全部主要样例的字段作 **2,286 次变异形状对照**，结果一致。它仅用于此次额外对照，测试集已移除其必需导入；业务校验仍由专门用例覆盖。
- 历史 0.1.0 wheel 及其哈希不代表本次零依赖版本，不应用它判断当前运行依赖。

## 已证明与未证明

已证明：最小记录格式可共享；精确版本引用不能用相似文本替代；消费子范围不能超出单元；部分阅读不能完成全部目标；页面不能漏掉相关候选的来源；needs-qa 不能在输出中丢掉草稿限制；schema/defaults 可随独立包分发。

未证明且不属于 P0：生产切片效果、实际 artifact 回读/完整性、原件解析质量、真实页面审核、registry 授权、并发任务提交、实际 tokenizer 计数、检索召回率。Protocol 是接口声明，不是 get/build 服务实现。CLI 成功结果也显式标出 source_integrity_verified=false、authorization_verified=false。

## 当前工作树与下一阶段

修改在 main 本地工作树；未提交、未推送、未合并 intranet，未安装到 Hermes/Provider 运行环境。原有未提交说明文档保留，没有执行任何 Vault 清理或重新摄取。本次新增包使用 `python3 -m` 入口，没有新增或修改 Skill scripts 下的入口文件。

接下来 P1 实现新 bootstrap 的库身份、契约配置、目录、模板和校验接线；P2 才实现来源结构、切片及真实读取。正式 ingest 仍在全链路实践完成后执行。
