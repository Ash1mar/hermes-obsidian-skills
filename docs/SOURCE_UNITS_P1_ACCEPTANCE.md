# P1：新库骨架与复制部署验收

日期：2026-09-14。范围：本地 main 实现；不迁移旧内容，不执行正式 Vault 重建或 ingest。

## 交付

- bootstrap 的 general、meeting、engineering 都生成 Vault 身份和治理注册表，保留原有治理契约。
- 新增 `_system/sources/{artifacts,sections,units}`、`_system/ledgers/unit-work` 和 `_system/knowledge-builds`。
- `_system/vault.json` 声明 `hermes-source-unit-vault/v1`、P1 能力和配置指纹。只有 bootstrap 可用，来源读取、知识构建、检索均为 false。
- `_system/metadata/source-unit-config.json` 保存 P0 已定义的来源、阅读和检索预算，避免多份预算漂移。显式完整 JSON 配置优先于内置默认值；未知字段和非法配置在落盘前拒绝。运行时读取 Vault 配置并校验指纹。
- bootstrap、lint 的 `lib/hermes_source_units` 是共享源码生成的内置副本，包括 schema/defaults。零第三方运行依赖，不增加服务或安装步骤。后续阶段重排后，Provider 分发属于 P5。
- 增加只读 `validate_source_unit_vault.py`。成功仅表示骨架有效，返回 `bootstrap_ready: true`、`query_ready: false`。
- 新知识页模板、生成的 AGENTS/ingest 规则和 Skill 说明明确 P1 阶段门禁。完整 lint 对新库报告 `source_units.pipeline_pending`，不得把空骨架判作摄取或查询就绪。
- 重复初始化默认拒绝；即使传 `--force-empty`，也不覆盖已有控制文件。不提供旧内容适配路径。

## 验证

新增 7 项测试覆盖：三个 profile 独立复制部署、`python -I -S` 环境、重复执行保持文件不变、完整配置覆盖、非法配置不落盘、配置指纹篡改、缺失目录、虚假能力声明、独立 lint 和内置副本一致性。

两份 Skill 的 quick_validate 通过；入口脚本 Git 模式验证为 100755。完整仓库测试结果见本次交付说明。

另在工作区 `tmp/source-units-p1-practice-20260914` 实际创建 engineering 试验库，并通过独立校验入口确认骨架有效、查询未就绪。试验库不是生产库。

## 后续边界

P1 验收时，P2 尚待实现规范文本、结构切分、精确坐标、SourceUnit 持久化和读取。后续计划现已重排为：P3 实现 Pass/Reduce 与 Build Finalize，P4 实现 Vault Finalize，P5 接入 Provider。P1 配置当时只可加载校验，P2 已开始驱动来源切片；检索模型仍留到 P5。

开发时修改共享源码后执行 `python3 hermes-source-units/tools/sync_skill_runtime.py`，再执行 `--check` 检测副本漂移。用户只复制完整 Skill 目录。
