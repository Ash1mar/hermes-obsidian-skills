# Retrieval Provider 运维说明

本文维护 Hermes Obsidian Skills 的粗召回 Provider 开关、配置层次和验证步骤。当前 Provider 是 `qmd-like-rag`，Skill 只依赖 `hermes-coarse-recall/v1` 协议。

## 三层配置

不要混淆以下文件：

1. 仓库默认配置：
   - query：`hermes-obsidian-controlled-query/config/retrieval-provider.json`
   - ingest：`hermes-obsidian-controlled-ingest/config/retrieval-provider.json`
   - `main` 的 query 默认已在完成模型、索引和 manifest 门禁后设为 `enabled: true`；ingest 仍默认关闭。
   - `intranet` 保持独立部署配置和开关，不继承 main 的主机路径或启用状态。
2. Hermes 当前主机的实际部署配置：
   - query：`/root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json`
   - ingest：`/root/.hermes/skills/domain/hermes-obsidian-controlled-ingest/config/retrieval-provider.json`
   - Hermes 每次运行适配脚本时读取这里的配置；调整当前主机状态应修改这一层。
3. Provider 主机配置：
   - main：`/root/.config/qmd-like-rag/main.json`
   - 这里维护模型、revision、设备、chunk 参数、检索参数和本地状态目录，不维护 Skill 的 `enabled` 开关。

重新部署或 rsync Skill 可能用仓库默认配置覆盖主机部署配置。部署完成后应重新执行本文件的状态检查和环境开关步骤。

## 开关含义

- query `enabled: false`：跳过 qmd-like-rag 粗召回分支；层级检索和传统检索继续工作。
- query `enabled: true`：query Skill 可以只读调用 `qmd-like-rag recall`；不会建立、同步或重建索引。
- ingest `enabled: true`：ingest Skill 可以通过 `sync_retrieval_index.py` 写入或同步 Provider 索引。
- 关闭适配器不会删除 Chroma、BM25、模型或 Vault manifest；重新开启后仍可使用兼容的 ready 索引。

## Query session 开发准则

`query_session.py` 是领域无关的会话状态机，它负责执行已显式选择的检索、核验、门禁和收口策略，不负责根据问题内容猜测策略。开发和评审时遵守以下边界：

- `query_session.py` 负责执行策略；
- Skill/调用方根据证据要求选择策略；
- 问题关键词、语言、设备名、参数名和已测样题不得参与生产策略判定。

例如，视觉原页核验必须由调用方显式传入 `--verification-required`。控制器只根据该状态准备已注册的 evidence image、viewer 或支持的页渲染载体，不得通过“参数”、“表格”、设备名或其他词表自动触发。测试应验证领域无关的状态转换和失败边界，不应把个别 trace 的问法固化为生产规则。

## 启用 query 前的门禁

在 WSL/Hermes 主机内确认：

```bash
qmd-like-rag doctor
qmd-like-rag status \
  --vault-root /mnt/c/Users/vimdr/Desktop/hermes-workspace/Hermes-HDJPSC-Fire-System-Vault \
  --config /root/.config/qmd-like-rag/main.json
```

只有同时满足以下条件才启用 query：

- `doctor.status` 为 `ok`；
- `status.status` 为 `ready`；
- `document_count` 和 `chunk_count` 大于零；
- `errors` 为空；
- Vault 的 `_system/reports/retrieval-index-manifest.json` 为 `ready`，且配置、模型和索引指纹与主机状态一致。

如果状态为 `absent`、`failed`、配置/模型不兼容或索引损坏，应由 ingest 维护流程处理；query 不得自行 sync 或 rebuild。

## 调整当前 main 主机的 query 状态

实际生效文件是：

```text
/root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json
```

先查看当前值：

```bash
grep -n '"enabled"' \
  /root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json
```

启用 query：

```bash
sed -i 's/"enabled": false/"enabled": true/' \
  /root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json
```

停用 query：

```bash
sed -i 's/"enabled": true/"enabled": false/' \
  /root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json
```

修改后验证 JSON 和最终值：

```bash
python3 -m json.tool \
  /root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json \
  >/dev/null

grep -n '"enabled"' \
  /root/.hermes/skills/domain/hermes-obsidian-controlled-query/config/retrieval-provider.json
```

适配脚本在每次 query 调用时重新读取 JSON，因此下一次 query 即可使用新值，通常不需要重启 Hermes。若修改后又重新部署 Skill，应再次检查该值。

## 启用后的验证

使用 query Skill 发起一个普通定位问题，检查 query trace 中：

- `coarse-recall` 路线不再显示 `disabled`；
- Provider 为 `qmd-like-rag`；
- 返回值具有 `authority: candidate-navigation-only`；
- 粗召回候选与层级候选完成融合；
- query 没有调用 `sync` 或 `--rebuild`；
- 最终答案仍打开并验证当前 Vault 文档或原始 PDF，而不是把向量命中直接当作证据。

如果 Provider 不可用，query 应记录 warning，并继续层级检索和传统检索。

## 仓库默认值策略

`main` 当前已满足 Provider ready 门禁，因此仓库 query 配置为 `enabled: true`。新部署在复制该配置前必须完成同样的 doctor、status 和 manifest 校验；未满足时应在部署层显式关闭 query，而不是允许 query 自行建立索引。

main 和 intranet 应分别维护各自主机的部署开关。intranet 默认仍关闭，还必须明确配置本地 command 或 HTTP transport，不能照搬 main 的主机路径或猜测服务地址。
