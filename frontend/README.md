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

默认使用 `$HERMES_HOME/skills/domain` 下已安装的 Skills、Finalize Skill 自带的 Provider 配置以及 `~/.local/state/qmd-like-rag`。控制台和已安装的短路径入口需使用支持同一暂停及接管协议的版本。可通过 `--skills-root`、`--provider-config` 和 `--provider-state` 匹配现有部署。控制台保存的请求、执行记录和输出默认在 `$HERMES_HOME/operator-console`。Hermes 未设置 `HERMES_HOME` 时使用 `~/.hermes`。同一控制台状态目录一次接收一轮执行。

## 工作流

1. 在新建表单中逐行输入 PDF 或文件夹地址。支持 WSL 绝对路径、Windows 盘符路径和当前发行版的 WSL 共享路径。文件夹递归收集 PDF。由于现有启动器以文件名导入原件，重复文件名需要先消除歧义。
2. 填写新的 Vault 完整路径与名称，再点击「开始处理」。目标可以是尚未创建的目录或空目录。执行器调用已安装的 `start_post_mineru_short_test.py`。源工作流停在其 `exact_plan` 检查点，短路径验证交接后继续。测试 Vault 合格发布页会同步 Provider，Query 保持关闭。
3. 在新建表单预设暂停点，或在运行时从尚未开始的节点点「在此节点前暂停」。执行器完成前置工作并验证边界后显示「已暂停」，此时可在该节点点击「继续并复用已有成果」。接管或继续前也可选好下一暂停点，执行器会在开始本轮时接收该目标。如果计划跳过无工作量的节点，执行器在下一个可用边界停下。
4. 在 DevTools 中切换原运行与恢复日志，查看业务工作流、原生看板、源暂停点和短路径回执数。

## 在 Obsidian 中添加 Vault

在 Vault 页复制「Linux Obsidian 目录」，点击「打开 Linux Obsidian 管理器」，选择「打开文件夹作为 Vault」，并粘贴 Linux 路径，例如 `/opt/data/hermes/TestFrontend`。首次添加后，「打开已添加的 Vault」通过后端启动 WSL 内的 Obsidian，按实际路径打开该目录中的 `README.md`。

Obsidian 通过 WSLg 显示窗口。控制台以 root 运行时，会选择唯一一个普通 Linux 用户启动图形应用，也可用 `--obsidian-user vimdr` 指定用户。后端会授予该用户所选 Vault 根目录的读写及遍历权限，以满足 Obsidian 的打开检查，并授予 `.obsidian` 设置目录的写入权限，用于保存应用工作区。文件选择器中可按 `Ctrl+L` 切换为路径输入框。Obsidian 启动输出记录在控制台状态目录中的 `obsidian.log`。Linux 应用和 Windows 版 Obsidian 分别维护自己的 Vault 列表。

| 节点 | 界面可提供的恢复 |
| --- | --- |
| 创建 Vault | 执行器尚未进入 Bootstrap 时可重试同一启动请求，例如 Gateway 当时不可用 |
| 来源准备、精确计划与交接 | 启动器退出且已创建来源工作流时，可重新接管同一账本，等待交接并核对原有来源成果 |
| 证据映射 | 有当前发布基线和受阻映射时，可用现有入口和绑定该 Build 的映射恢复清单 |
| 页面协调 | 首轮未发布且保存了已停止的协调暂缓结果时，重放协调 |
| 分段写作 | 未发布时重放已返回的写作。已发布部分结果时可走现有受阻内容恢复 |
| 正式构建 | 写作授权已落盘时尝试交付恢复。对仍是当前发布的原始已验收结果，可重放正式构建 |
| 发布与索引 | 初次正式构建结果已落盘后，可复用现有 Build 恢复交付和验收 |

「来源准备」前暂停时，Bootstrap 已登记并导入原件，尚未提交原生转换。「精确计划与交接」前暂停时，原生工作流已经停在 `exact_plan`，短路径在验证来源资格前等候继续。「发布与索引」前暂停会保留正式 Build，「最终验收」前暂停会保留已有发布和 Provider 索引。

PDF 独立排队和计转换时间。启动器等待交接时记录最近一次原生进展，来源明细显示每份 PDF 的状态及可用的运行观察时间。所有文件终结后，成功与失败并存时来源准备显示「部分完成」。无原生进展时启动器可以退出观察，来源账本仍保留原来的状态。重新接管只绑定已有来源工作流。若原生任务已经停滞，需先检查 DevTools 和来源账本并按原生恢复入口处理。

写作重放适用于原生入口认可的已返回尝试。映射恢复清单的格式为 `{"requests": ["请求 ID"], "prior_build": "Build ID"}`，应由操作员从受影响的原生回执确定，实际范围由短路径入口复核。

## 查看旧运行

可挂载采用同一目录布局的既有运行报告。历史记录只开放查看。Windows 上可用以下命令预览界面，也可以在 WSL 输入这些目录对应的路径。

```powershell
python -B frontend/server.py --read-only --history ../tmp/testvault-e2e9-supplement3-20261112
```

工作流页面显示运行和各节点。历史 Vault 若不在当前文件系统中，控制台用已保存的交接和验收文件显示当时的状态。
