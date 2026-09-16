# P4 Vault Finalize 验收

日期：2026-09-16。范围：临时新 Vault 的文件式实践；未部署 WSL、未创建正式库、未同步 Provider。

P4 新增 `FileVaultFinalizeService` 及独立 `hermes-obsidian-knowledge-finalize` Skill。它在 P3 Build Finalize 之后执行 `plan → apply → validate`，持久化 release state、版本化 navigation 和最后提交的 knowledge release manifest。

验收覆盖：

- completed build 的精确 revision 与 identity registry 钉住；
- UnitSet 替换只标记对应旧贡献 stale，保留其他 current 支持；
- 来源撤回且没有剩余支持时页面进入 blocked，不自动删除；
- 页面移动保持 page_id，并只在旧内容哈希可信时写 redirect；
- 别名、路径、wikilink、反向链接和目录投影；死链阻止 apply；
- SourceUnit 与知识页分别输出索引资格及明确原因；P4 不批准业务状态；
- apply 前完整重算与 revision/hash 检查，release manifest 最后写入；
- 重试幂等、历史 release 可验证、当前 navigation 可核对；
- Bootstrap 建立 P4 目录与空 release state，Lint 只读验证 release；
- Finalize Skill 在 `python -I -S` 下只使用其内嵌标准库运行时。

验证结果：仓库全量测试 `226 passed`；共享运行时同步、Python 编译和 Git whitespace 检查通过。新增 CLI 按 Git `100755` 交付。

阶段结论：P4 已形成可审计的 Vault 级收尾边界。P5 将消费 release 的 index eligibility，改造 Provider/query 直接投影 canonical Unit；P4 本身不调用 Provider。
