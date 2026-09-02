# Main → Intranet 分支维护合同

## 目标

`main` 是共享实现、Skill、Provider、测试和公共文档的主线。`intranet` 通过 merge 持续包含
`main`，同时保留可直接下载部署的内网配置。完成首次历史基线 merge 后，日常维护不再通过
worktree 或 cherry-pick 复制共享提交。

## 标准流程

开始前要求工作树干净，并刷新远端引用。

```powershell
git fetch --prune origin
git switch main
git merge --ff-only origin/main
```

在 `main` 完成共享修改、测试、提交和推送：

```powershell
git add <scoped-paths>
git commit -m "<type(scope): summary>"
git push origin main
```

然后在同一个干净工作树中将 `main` 合入 `intranet`：

```powershell
git switch intranet
git merge --ff-only origin/intranet
git merge --no-ff main
```

验证内网默认行为后推送，并切回 `main`：

```powershell
git push origin intranet
git switch main
```

## 受保护的 Intranet 差异

下面的文件表达部署差异。merge 时默认保留 `intranet` 的有效值，除非任务明确要求修改内网部署：

- `hermes-obsidian-controlled-ingest/config/deployment.json`
- `hermes-obsidian-controlled-ingest/config/retrieval-provider.json`
- `hermes-obsidian-controlled-query/config/deployment.json`
- `hermes-obsidian-controlled-query/config/retrieval-provider.json`
- `hermes-obsidian-vault-bootstrap/config/deployment.json`
- `hermes-obsidian-vault-lint/config/deployment.json`

当前合同要求继续保留固定 Vault、Skills 根目录、MinerU HTTP、viewer URL，以及默认禁用的
intranet Provider adapter。共享 Skill 和脚本不得重新硬编码这些值。

以下测试包含分支专用断言，发生冲突时必须同时合入 `main` 新增的共享测试和内网部署断言，
不能整文件选择任意一侧：

- `tests/test_deployment_profiles.py`
- `tests/test_hierarchical_query.py`
- `tests/test_retrieval_provider.py`

## 变更归属

- 共享代码、Skill、Provider、协议、schema、UI 元数据和公共文档：先改 `main`，再 merge。
- 内网地址、固定路径、Provider 开关或明确的内网专用行为：可以直接提交到 `intranet`。
- 在内网使用中发现的共享缺陷：回到 `main` 修复并验证，再 merge 到 `intranet`。
- 不把 `intranet` merge 回 `main`，避免部署配置反向进入共享主线。

## 冲突和停止条件

- 共享文件应采用 `main` 的新实现，并重新应用必要的配置驱动行为。
- 受保护配置应保留 `intranet`，除非有明确部署变更。
- 测试文件必须语义合并，不能用整文件 `ours`/`theirs` 掩盖新测试。
- 如果冲突超出受保护文件或会改变 Vault、Provider、证据或只读边界，停止并重新审计差异，
  不要改回 cherry-pick 工作流规避冲突。

## 验收

每次双分支更新至少确认：

1. `main` 和 `intranet` 各自完整测试通过；
2. 四个 Skill 通过结构校验；
3. Skill `scripts/` 入口保持 shebang 和 Git `100755`；
4. `intranet` 仍通过无运行时参数的部署配置测试；
5. Provider 未部署时，禁用 adapter 不启动命令、模型或索引；
6. `git merge-base --is-ancestor main intranet` 成功，证明 `intranet` 已包含当前 `main`。

Worktree 只用于用户明确要求的并行隔离任务，或普通的干净切换流程确实无法执行时；它不是双分支
日常同步机制。
