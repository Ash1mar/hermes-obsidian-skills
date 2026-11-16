# Hermes 本地控制台

在 Windows PowerShell 中用这一条命令启动控制台和 Hermes Gateway。

```powershell
wsl.exe --distribution Ubuntu-24.04 --user root --exec /usr/local/lib/hermes-agent/venv/bin/python3 -B /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/frontend/server.py
```

也可以在 Hermes 所在的 WSL 发行版里，以现有 Hermes 运行账户执行：

```bash
cd /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills
/usr/local/lib/hermes-agent/venv/bin/python3 -B frontend/server.py
```

在 Windows 浏览器打开控制台打印的本机地址。默认端口为 `18465`，可用 `--port` 调整。服务只绑定 `127.0.0.1`，面向同一台机器上的单人操作。若 WSL 的 localhost 转发被禁用，请用 WSL 内的浏览器或恢复该转发。

控制台启动时先检查当前 Hermes profile 的 Gateway，复用已有进程，或运行 `hermes gateway run`。页面顶部显示检查、启动、就绪等状态，点击可进入 DevTools 查看进程 PID、启动来源和本次启动日志。Gateway 就绪后即可提交新流程。打开控制台只启动 Gateway，工作流由提交操作触发。Windows 的历史查看模式不启动 Gateway。

关闭控制台时 Gateway 继续在后台运行，以便处理已提交的工作流。后续启动控制台会复用该进程。每个运行由单独的本机执行器持有，刷新浏览器或重启 HTTP 服务后仍可查看同一运行。

默认使用 `$HERMES_HOME/skills/domain` 下已安装的 Skills、Finalize Skill 自带的 Provider 配置以及 `~/.local/state/qmd-like-rag`。可通过 `--skills-root`、`--provider-config` 和 `--provider-state` 匹配现有部署。控制台保存的请求、执行记录和输出默认在 `$HERMES_HOME/operator-console`。Hermes 未设置 `HERMES_HOME` 时使用 `~/.hermes`。同一控制台状态目录一次接收一轮执行。

## 工作流

1. 在新建表单中逐行输入 PDF 或文件夹地址。支持 WSL 绝对路径、Windows 盘符路径和当前发行版的 WSL 共享路径。文件夹递归收集 PDF。由于现有启动器以文件名导入原件，重复文件名需要先消除歧义。
2. 填写新的 Vault 完整路径与名称，再点击「开始处理」。目标可以是尚未创建的目录或空目录。执行器调用已安装的 `start_post_mineru_short_test.py`。源工作流停在其 `exact_plan` 检查点，短路径验证交接后继续。测试 Vault 合格发布页会同步 Provider，Query 保持关闭。
3. 在流程节点里查看实时进度及支持的恢复操作。状态来自启动回执、源账本、短路径阶段计划与度量、正式交付和最终验收。判断是否合格、复用输入和执行模型调用由现有 Hermes 程序负责。
4. 在 DevTools 中切换原运行与恢复日志，查看业务工作流、原生看板、源暂停点和短路径回执数。Vault 页提供 Obsidian URI。请先在 Obsidian 注册对应的 Vault 名称。

| 节点 | 界面可提供的恢复 |
| --- | --- |
| 创建 Vault | 执行器尚未进入 Bootstrap 时可重试同一启动请求，例如 Gateway 当时不可用 |
| 证据映射 | 有当前发布基线和受阻映射时，可用现有入口和绑定该 Build 的映射恢复清单 |
| 页面协调 | 首轮未发布且保存了已停止的协调暂缓结果时，重放协调 |
| 分段写作 | 未发布时重放已返回的写作。已发布部分结果时可走现有受阻内容恢复 |
| 正式构建 | 写作授权已落盘时尝试交付恢复。对仍是当前发布的原始已验收结果，可重放正式构建 |
| 发布与索引 | 初次正式构建结果已落盘后，可复用现有 Build 恢复交付和验收 |

来源准备、精确计划、初始主题和最终验收没有各自独立的通用重跑按钮。写作重放只适用于原生入口认可的已返回尝试。映射恢复清单的格式为 `{"requests": ["请求 ID"], "prior_build": "Build ID"}`，应由操作员从受影响的原生回执确定，实际范围由短路径入口复核。

## 查看旧运行

可挂载采用同一目录布局的既有运行报告。历史记录只开放查看。Windows 上可用以下命令预览界面，也可以在 WSL 输入这些目录对应的路径。

```powershell
python -B frontend/server.py --read-only --history ../tmp/testvault-e2e9-supplement3-20261112
```

工作流页面显示运行和各节点。历史 Vault 若不在当前文件系统中，控制台用已保存的交接和验收文件显示当时的状态。
