# MinerU 在 WSL2 中的安装、排障与迁移经验

> 适用场景：Hermes 在 Windows 主机上运行，通过 WSL2 调用 MinerU 处理工程 PDF，并输出受控摄取使用的 Document Bundle。
>
> 本文记录从 Hermes 初始环境检查报错，到模型补齐、pipeline 验证、vLLM 冷启动排障，以及最终迁移到 WSL 原生文件系统的完整过程。

## 1. 最终结论

本次问题不是单一故障，而是三个相互独立的问题叠加：

1. MinerU pipeline 模型缓存不完整，缺少 OCR 识别模型和公式识别模型。
2. MinerU 没有稳定的本地模型配置，在线下载又受到 TLS、代理和下载源速度影响。
3. MinerU 虚拟环境位于 `/mnt/c`，vLLM 冷启动需要读取大量 Python 文件和 CUDA 扩展，触发 WSL Plan 9 文件访问瓶颈，进程长期停留在 `p9_client_rpc`。

最终结构如下：

```text
Windows 工作区
C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills
  ├─ MinerU Bundle 转换脚本
  ├─ Bundle 验证器
  └─ 原 .venv-mineru/              # 暂时保留，只作回滚副本

WSL 原生文件系统
/root/.venvs/mineru/               # 当前实际使用的 MinerU 环境
/root/.cache/huggingface/hub/...   # pipeline 与 VLM 模型
/root/mineru.json                  # 本地模型路径配置
/usr/local/bin/mineru              # 统一 CLI 包装器
/root/.hermes/skills/domain/
  └─ hermes-obsidian-controlled-ingest/
```

当前已验证：

- MinerU：3.3.1
- Python：3.12.3
- PyTorch：2.11.0，CUDA 13.0 构建
- vLLM：0.21.0
- Transformers：4.57.6
- GPU：NVIDIA GeForce RTX 5070 Ti Laptop GPU
- `pip check`：无损坏依赖
- CUDA 可见且 GPU 张量计算成功
- `mineru --version` 正常
- vLLM 导入约 7～9 秒
- MinerU pipeline 使用真实工程 PDF 首页测试成功
- MinerU hybrid-engine 使用同一份真实工程 PDF 第 1 页测试成功
- Bundle schema 2.0 与 Bundle 验证器均通过
- Hermes 已发现并启用更新后的本地 skill

最终 hybrid 测试确认：迁移消除了 `p9_client_rpc` 阻塞；补齐迁移权限、CUDA JIT 工具链与标准 Toolkit 目录布局后，vLLM EngineCore、FlashInfer、CUDA Graph、页面推理、Bundle v2 打包和验证均可完成。

## 2. Hermes 初始报告中的信息

Hermes 最初检查得到：

- MinerU CLI 已安装，版本为 3.3.1。
- 虚拟环境位于 Windows 工作区：

  ```text
  /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/.venv-mineru
  ```

- PyTorch 能识别 CUDA 和 RTX 5070 Ti。
- Hugging Face 模型缓存不完整。
- 在线下载出现 SSL EOF 或下载中断。
- 报告怀疑存在 Hermes 与 MinerU 的 Python 依赖冲突。
- pipeline 与 hybrid 均缺少完整冒烟测试。

这份报告提供了方向，但不能直接把报告中的每一条判断当作事实。后续实际检查发现，其中至少有两项已经过时或不准确。

## 3. 第一条经验：始终以当前环境为准，不直接修复报告中的推测

### 3.1 Python 依赖冲突是旧状态

初始报告提到 `hermes-agent` 与 MinerU 存在依赖冲突。实际在 `.venv-mineru` 中检查：

```bash
/path/to/.venv-mineru/bin/python -m pip check
/path/to/.venv-mineru/bin/python -m pip show hermes-agent
```

结果是：

- `hermes-agent` 并未安装在 MinerU 环境中。
- `pip check` 没有发现损坏依赖。

因此没有为了“解决冲突”盲目升级或降级包。

经验：

- 报告是诊断线索，不是当前状态快照。
- 修改依赖前必须重新执行 `pip check`、`pip show` 和实际 import。
- Hermes 与 MinerU 应保持独立虚拟环境，不要把 `hermes-agent` 安装进 MinerU venv。

### 3.2 报告中提到的 OCR 文件名并不适用于当前版本

报告最初怀疑缺少：

```text
ch_PP-OCRv4_det_server_infer.pth
```

但检查 MinerU 3.3.1 自带的 `models_config.yml` 后，当前 pipeline 实际要求的是：

```text
models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth
models/OCR/paddleocr_torch/ch_PP-OCRv4_rec_server_doc_infer.pth
```

其中：

- v5 detection 模型已经存在。
- v4 document recognition 模型缺失。

经验：

- 不要根据旧教程或错误日志猜模型文件。
- 先读取当前安装版本自己的模型配置。
- detection、recognition、公式识别和版面模型是不同组件，文件名相似但不能互换。

## 4. 模型缓存修复

### 4.1 已存在的模型快照

pipeline 模型目录：

```text
/root/.cache/huggingface/hub/
  models--opendatalab--PDF-Extract-Kit-1.0/
  snapshots/ed6b654c018d742e65a17671e379c5e6ecc87ec9
```

VLM 模型目录：

```text
/root/.cache/huggingface/hub/
  models--opendatalab--MinerU2.5-Pro-2605-1.2B/
  snapshots/bff20d4ae2bf202df9f45284b4d43681555a97ed
```

VLM 快照已经完整，主要权重文件约 2.3 GB，因此不需要重复下载整个 VLM 模型。

### 4.2 实际补齐的文件

补齐了以下关键文件：

```text
models/OCR/paddleocr_torch/
  ch_PP-OCRv4_rec_server_doc_infer.pth

models/MFR/pp_formulanet_plus_m/
  PP-FormulaNet_plus-M.pth
  PP-FormulaNet_plus-M_inference.yml
```

已确认存在的 detection 文件：

```text
models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth
```

### 4.3 下载源方面的经验

本次遇到的情况：

- Hugging Face 默认链路出现 SSL EOF。
- ModelScope 整体快照下载非常慢，并出现重复进程等待锁。
- Hugging Face 镜像 API 可以访问。
- `huggingface_hub.snapshot_download` 在镜像的 HEAD 元数据检查阶段失败。
- 根据镜像 API 返回的精确路径和大小，使用直接 HTTP 下载成功。

经验：

- 大模型下载失败时，不要反复启动完整 snapshot 下载，否则容易产生锁等待和重复流量。
- 先判断缺的是单个文件还是整个仓库。
- 若仅缺少少量文件，按模型配置和仓库 API 精确下载。
- 下载后同时验证路径、文件大小和加载行为。
- 模型准备完成后，转换阶段应使用 `local`，避免运行时依赖网络。

## 5. 建立离线本地模型配置

创建 `/root/mineru.json`：

```json
{
  "config_version": "1.3.1",
  "models-dir": {
    "pipeline": "/root/.cache/huggingface/hub/models--opendatalab--PDF-Extract-Kit-1.0/snapshots/ed6b654c018d742e65a17671e379c5e6ecc87ec9",
    "vlm": "/root/.cache/huggingface/hub/models--opendatalab--MinerU2.5-Pro-2605-1.2B/snapshots/bff20d4ae2bf202df9f45284b4d43681555a97ed"
  }
}
```

统一入口 `/usr/local/bin/mineru`：

```bash
#!/usr/bin/env bash
set -euo pipefail

export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-local}"
exec /root/.venvs/mineru/bin/mineru "$@"
```

这样做的好处：

- Hermes 和人工操作都使用同一个命令。
- 默认使用本地模型，不受网络波动影响。
- 虚拟环境位置变化时，只需修改包装器。
- Bundle 转换脚本无需绑定具体 venv 路径。

## 6. pipeline 冒烟测试的结果

模型补齐后，使用真实工程 PDF 的第一页运行 pipeline：

```text
backend: pipeline
model source: local
pages: 1
```

结果：

```text
Bundle schema: 2.0
Profile: engineering
Conversion quality: pass
Bundle validation: pass
```

这一步证明：

- OCR 模型可加载。
- 公式模型文件不再缺失。
- GPU pipeline 可工作。
- `/root/mineru.json` 生效。
- MinerU 到 Document Bundle v2 的完整链路可用。

pipeline 首次启动仍需要模型初始化，但日志能够持续前进，并最终完成。首次启动慢和永久卡死必须通过日志、CPU、GPU 与进程等待状态区分。

## 7. hybrid-engine 冷启动问题

### 7.1 表现

`hybrid-engine` 能够：

- 启动本地 MinerU API。
- 接收任务。
- 选择 `vllm-async-engine`。
- 识别 GPU compute capability 12.0。

但之后长时间没有进入实际 GPU 推理，GPU 显存占用仍为 0。进程状态显示：

```text
state: D
wchan: p9_client_rpc
```

同时，简单执行 `du -sh` 遍历原虚拟环境也需要数分钟。

### 7.2 根因

原虚拟环境位于：

```text
/mnt/c/.../.venv-mineru
```

`/mnt/c` 通过 WSL 的 Windows 文件共享层访问。普通脚本读取问题不明显，但 vLLM 冷启动会读取大量内容：

- Python 模块与元数据。
- Torch、vLLM 和 Transformers 包。
- CUDA 扩展和共享库。
- 大量零散的小文件。

这类工作负载会放大 WSL 跨文件系统的元数据开销，使进程阻塞在 `p9_client_rpc`。因此该现象更接近文件系统 I/O 卡住，而不是 CUDA 驱动错误、模型缺失或显存不足。

## 8. 虚拟环境迁移过程

### 8.1 为什么没有直接重新安装

原环境已经通过 pipeline 验证，而且包含 8.3 GB 依赖。重新安装存在以下风险：

- 重新下载 Torch、vLLM 和 CUDA 相关大包。
- 网络仍可能出现 TLS 或代理问题。
- 依赖求解可能得到不同版本。
- 新环境与已经验证的环境产生行为差异。

因此采用“迁移已验证环境，再修复路径”的策略。

### 8.2 第一次尝试：WSL rsync

目标目录：

```text
/root/.venvs/mineru
```

使用 `rsync -a` 从 `/mnt/c` 复制。该方法可靠且可续传，但仍需逐个通过 WSL 文件共享层读取小文件，速度过慢。

迁移初期表现为：

- 数分钟仅复制数百 MB。
- 终止信号需要等待不可中断的 I/O 返回后才生效。
- 进一步证实 `/mnt/c` 小文件访问就是瓶颈。

### 8.3 最终方法：Windows 原生打包，WSL 顺序解包

采用以下思路：

1. 在 Windows 侧原生遍历 NTFS 虚拟环境。
2. 生成单个未压缩 tar 传输归档。
3. WSL 从 `/mnt/c` 顺序读取一个大文件。
4. 在 WSL 原生 ext4 中解包。
5. 重建符号链接和入口脚本。

Windows `bsdtar` 无法读取 WSL 创建的部分 reparse point：

```text
lib64
bin/python
bin/python3
bin/python3.12
```

解决方法是：

- 归档 `lib`、`include`、`share`、`bin`、`.lock` 和 `pyvenv.cfg`。
- 忽略无法归档的 Python 与 `lib64` 符号链接。
- 在 WSL 中重新创建：

  ```bash
  ln -sfn /usr/bin/python3.12 /root/.venvs/mineru/bin/python
  ln -sfn python /root/.venvs/mineru/bin/python3
  ln -sfn python /root/.venvs/mineru/bin/python3.12
  ln -sfn lib /root/.venvs/mineru/lib64
  ```

这种方法把大量随机小文件访问转换成一次顺序读取，迁移过程明显更稳定。

### 8.4 修复不可迁移路径

Python venv 中的 console scripts 会把解释器绝对路径写入 shebang，例如：

```text
#!/mnt/c/.../.venv-mineru/bin/python3
```

迁移后将其替换为：

```text
#!/root/.venvs/mineru/bin/python3
```

本次共重写 86 个入口脚本。

还需要修改以下激活脚本中的 `VIRTUAL_ENV`：

```text
bin/activate
bin/activate.csh
bin/activate.fish
```

仅修复 shebang 不够；必须搜索 `bin/` 和 `pyvenv.cfg` 中是否仍有旧路径。

## 9. 迁移后的验证

### 9.1 路径验证

```bash
command -v mineru
mineru --version
```

期望：

```text
/usr/local/bin/mineru
mineru, version 3.3.1
```

Python 检查：

```bash
/root/.venvs/mineru/bin/python - <<'PY'
import sys
print(sys.prefix)
print(sys.executable)
print([p for p in sys.path if p.startswith('/mnt/c/')])
PY
```

期望：

```text
/root/.venvs/mineru
/root/.venvs/mineru/bin/python
[]
```

### 9.2 依赖验证

```bash
/root/.venvs/mineru/bin/python -m pip check
```

期望：

```text
No broken requirements found.
```

### 9.3 vLLM 和 CUDA 验证

```bash
/root/.venvs/mineru/bin/python - <<'PY'
from time import perf_counter

t0 = perf_counter()
import vllm
t1 = perf_counter()
import torch

print('vllm import seconds:', round(t1 - t0, 3))
print('cuda:', torch.cuda.is_available())
print('device:', torch.cuda.get_device_name(0))
print(torch.ones(1, device='cuda').item())
PY
```

本次结果：

```text
vllm import seconds: 7～9
cuda: True
device: NVIDIA GeForce RTX 5070 Ti Laptop GPU
cuda tensor: 1.0
```

### 9.4 旧路径检查

```bash
grep -RIl \
  '/mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills/.venv-mineru' \
  /root/.venvs/mineru/bin \
  /root/.venvs/mineru/pyvenv.cfg
```

最终结果应为空。

## 10. Hermes 和 skill 集成

Hermes 注册目录：

```text
/root/.hermes/skills/domain/hermes-obsidian-controlled-ingest
```

验证命令：

```bash
hermes skills list
```

期望状态：

```text
hermes-obsidian-controlled-ingest | domain | local | enabled
```

skill 中不再推荐使用 `/mnt/c/.../.venv-mineru/bin/mineru`，统一改为：

```text
/usr/local/bin/mineru
```

Bundle 转换和验证脚本主要使用 Python 标准库，可以由 WSL 系统 Python 执行；真正沉重的 MinerU、Torch 和 vLLM 依赖通过 `/usr/local/bin/mineru` 进入原生 venv。

推荐调用形式：

```bash
cd /mnt/c/Users/vimdr/Desktop/hermes-workspace/hermes-obsidian-skills

python3 \
  hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py \
  "/mnt/c/path/to/input.pdf" \
  -o "/mnt/c/path/to/output_document_bundle" \
  --mineru-command /usr/local/bin/mineru \
  --model-source local \
  --backend pipeline \
  --method auto \
  --lang ch \
  --overwrite
```

## 11. 推荐的全新安装路线

如果从零开始，不应先把 MinerU venv 建在 Windows 工作区再迁移。推荐：

1. 在 WSL 原生目录创建 venv：

   ```bash
   python3.12 -m venv /root/.venvs/mineru
   ```

2. 只在该环境中安装 MinerU 及其 GPU 依赖。
3. 不安装 `hermes-agent`。
4. 将 pipeline 和 VLM 模型放在 `/root/.cache` 或其他 WSL 原生目录。
5. 创建 `/root/mineru.json`，显式配置本地模型根目录。
6. 创建 `/usr/local/bin/mineru` 包装器，并默认设置 `MINERU_MODEL_SOURCE=local`。
7. 按以下顺序验证：

   ```text
   pip check
     → torch import
     → CUDA tensor
     → vLLM import
     → mineru --version
     → pipeline 小样本
     → hybrid 非敏感小样本
     → 完整工程文档
   ```

8. 只把项目脚本、Bundle 输出和需要与 Windows 共享的文件放在 `/mnt/c`。

## 12. 常见误区

### 误区一：CUDA 可用就说明 hybrid 一定可用

`torch.cuda.is_available()` 只能证明基础 CUDA 链路可用。vLLM 还涉及大量包加载、模型初始化、CUDA 扩展和异步引擎启动。

### 误区二：冷启动慢一定是模型或驱动错误

需要同时观察：

- MinerU 日志是否持续前进。
- GPU 显存和利用率。
- 进程 CPU 使用率。
- 进程状态和 `wchan`。
- 文件系统位置。

`D` 状态加 `p9_client_rpc`，并且环境位于 `/mnt/c`，优先怀疑 WSL 文件访问。

### 误区三：缺模型就重新下载整个模型仓库

先根据当前版本的配置找出精确缺失文件。重复下载完整快照会浪费时间，并可能引入下载锁和不完整缓存。

### 误区四：复制 venv 后只改 `python` 符号链接

还必须处理：

- console script shebang。
- `activate` 系列脚本。
- `pyvenv.cfg`。
- 旧路径残留。
- 可执行权限。

### 误区五：把 pipeline 通过等同于 hybrid 通过

两者依赖路径不同。pipeline 成功只能证明 pipeline 模型和基础 GPU 链路正常；hybrid 仍需要独立验收。

## 13. 最终 hybrid-engine 单页验收

2026-06-24 使用与 pipeline 测试相同的真实工程 PDF，只处理第 1 页。最终结果：

```text
backend: hybrid-engine
model source: local
pages: 1
MinerU task: completed
Bundle schema: 2.0
Bundle profile: engineering
Bundle quality: pass
Bundle validation: pass
sections: 3
```

成功运行的关键时间：

- 本地 MinerU API 启动约 6 秒。
- vLLM predictor 冷启动约 245 秒。
- EngineCore profile、KV cache 和 warmup 约 189 秒。
- 模型权重约 2.15 GiB，权重加载约 5 秒。
- 单页 Two Step Extraction 约 104 秒。
- 从启动到 MinerU 完成约 6 分 17 秒。

冷启动期间暴露并修复了以下迁移后问题：

1. Windows tar 丢失 Triton、tokenspeed_triton 和 Torch 内部工具的执行权限，导致 `ptxas-blackwell` `PermissionError`。
2. FlashInfer JIT 需要完整 nvcc，补充并固定：

   ```text
   nvidia-cuda-nvcc==13.0.88
   nvidia-nvvm==13.0.88
   nvidia-cuda-crt==13.0.88
   nvidia-cuda-cccl==13.0.85
   ```

3. `/usr/local/bin/mineru` 必须把原生 venv 的 `bin` 加入 `PATH`，否则子进程找不到 `ninja`。
4. pip CUDA Toolkit 使用 `lib/`，FlashInfer 按标准 Toolkit 查找 `lib64/`；补充了 `lib64 -> lib`。
5. JIT 链接需要 `lib64/stubs/libcuda.so`，将其链接到 WSL 驱动库 `/usr/lib/wsl/lib/libcuda.so`。
6. pip CUDA runtime 只有带版本号的 `.so`，为 JIT 链接补充未版本化的库符号链接，例如 `libcudart.so -> libcudart.so.13`。

最终包装器需要提供：

```bash
export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-local}"
export CUDA_HOME="${CUDA_HOME:-/root/.venvs/mineru/lib/python3.12/site-packages/nvidia/cu13}"
export PATH="/root/.venvs/mineru/bin:$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib:${LD_LIBRARY_PATH:-}"
exec /root/.venvs/mineru/bin/mineru "$@"
```

非阻断性警告包括：Transformers v4 将被未来 vLLM 版本弃用、WSL 使用 `pin_memory=False`，以及进程结束时 NCCL 的 `destroy_process_group()` 提示。这些警告没有影响本次任务完成和资源退出。

最终通过验证的 Bundle 位于：

```text
C:\Users\vimdr\Desktop\hermes-workspace\tmp\mineru-smoke\HDJPSC-25A4-02-02核岛消防系统设计工作手册_document_bundle
```

原 Windows venv 目前仍保留：

```text
C:\Users\vimdr\Desktop\hermes-workspace\hermes-obsidian-skills\.venv-mineru
```

hybrid 单页验收已经通过。原环境仍可短期保留作回滚副本；确认后续多页运行稳定后，可以删除以释放约 8.3 GB Windows 磁盘空间。

## 14. 为什么 hybrid-engine 会连续暴露这么多问题

这里的“high-engine”实际指 MinerU 的 `hybrid-engine`。它看起来只是一个 backend 参数，但实际执行链远比 pipeline 深：

```text
Hermes / Bundle 转换器
  → MinerU CLI
  → 本地 MinerU FastAPI
  → hybrid analyzer
  → vLLM 异步多进程 EngineCore
  → Qwen2-VL 多模态模型
  → PyTorch / TorchInductor
  → FlashAttention / FlashInfer / Triton
  → ninja + nvcc + CCCL
  → CUDA runtime + WSL GPU 驱动
  → Bundle v2 打包与验证
```

任何一层缺失，最终在上层通常只表现为笼统的：

```text
RuntimeError: Engine core initialization failed
```

必须向前查找 EngineCore 子进程打印的第一条异常，最后一条 `Engine core initialization failed` 只是汇总结果。

### 14.1 pipeline 通过为什么不能证明 hybrid 可用

pipeline 主要加载已有的版面、OCR、表格和公式模型。只要模型文件、Torch 和基础 CUDA 正常，它通常可以运行。

hybrid-engine 额外引入：

- vLLM 异步 EngineCore 与 `spawn` 多进程。
- 多模态 Qwen2-VL 模型。
- TorchInductor 图编译与 CUDA Graph profiling。
- FlashInfer sampling 扩展的运行时 JIT 编译。
- Triton 的 Blackwell `sm_120f` kernel 编译。
- nvcc、ptxas、ninja、CCCL 头文件和动态链接器。

因此以下检查都只覆盖了部分链路：

```text
torch.cuda.is_available() == True   # 只证明基础 CUDA runtime
GPU 张量成功                         # 只证明 PyTorch 能调用 GPU
import vllm 成功                     # 只证明 Python 包可以导入
pipeline 成功                        # 不会覆盖完整 VLM JIT 链
```

只有实际初始化 EngineCore 并完成一次 hybrid 推理，才能证明整条链路可用。

### 14.2 为什么问题是一个接一个出现，而不是一次全部报告

vLLM 和 FlashInfer 使用延迟初始化：

1. 先导入 Python 包。
2. 再创建 API 与 EngineCore 子进程。
3. 再加载模型权重。
4. 再运行 profile，计算 KV cache。
5. profile 过程中才调用 Triton、ptxas、FlashInfer 和 nvcc。
6. nvcc 编译成功后才进入本地链接。
7. EngineCore 完成后才开始处理 PDF 页面。

后一阶段只有在前一阶段成功后才会执行。因此修复 `ptxas` 权限后才会暴露缺少 `nvcc`；补上 nvcc 后才会暴露 `ninja`；ninja 能运行后才会发现 CCCL 头文件和链接目录问题。

这不是同一个错误反复变化，而是原环境从未真正走到过这些更深的执行层。

### 14.3 WSL 迁移为什么会引入权限和链接问题

原 venv 位于 NTFS `/mnt/c`。为了避免数十万次 Plan 9 小文件访问，迁移使用了 Windows 原生 tar 加 WSL 顺序解包。

该方法保留了文件内容，但 Windows tar 对 Linux POSIX 元数据支持不完整：

- 内部工具的执行权限由 `0755/0777` 变成 `0666`。
- Python 和 `lib64` 符号链接无法直接归档。
- pip CUDA 库中需要的未版本化 `.so` 链接不存在。

所以迁移后的普通 Python import 和 CUDA 张量可以成功，但第一次真实 JIT 调用仍会失败。

迁移 venv 时，不能只验证 `python` 和 `pip check`，还必须验证内部编译工具和动态链接布局。

### 14.4 pip CUDA 包不是标准 `/usr/local/cuda` Toolkit 布局

当前环境虽然已有：

```text
cuda-toolkit==13.0.2
nvidia-cuda-runtime==13.0.96
nvidia-cuda-nvrtc==13.0.88
```

但这不等于存在系统级 `/usr/local/cuda`，也不保证有 `nvcc`。

NVIDIA pip 包采用模块化布局：

```text
/root/.venvs/mineru/lib/python3.12/site-packages/nvidia/cu13/
  bin/
  include/
  lib/
  nvvm/
```

FlashInfer 则按传统 CUDA Toolkit 假设查找：

```text
$CUDA_HOME/bin/nvcc
$CUDA_HOME/lib64
$CUDA_HOME/lib64/stubs/libcuda.so
```

因此必须在包装器中设置 `CUDA_HOME`，并为 pip Toolkit 补充标准兼容布局。

### 14.5 RTX 5070 Ti / Blackwell 放大了工具链要求

GPU compute capability 为 12.0，Triton 使用：

```text
compute_120f
sm_120f
ptxas-blackwell
```

这要求较新的 CUDA 13、Triton 和 ptxas。旧 CUDA Toolkit、只包含 runtime 的安装，或者没有 Blackwell ptxas 的环境都无法完成该 JIT 路径。

因此最终将编译组件固定在 CUDA 13.0 系列，而不是任由 pip 把 nvcc 13.0 与 NVVM/CRT 13.3 混装：

```text
nvidia-cuda-nvcc==13.0.88
nvidia-nvvm==13.0.88
nvidia-cuda-crt==13.0.88
nvidia-cuda-cccl==13.0.85
```

### 14.6 本次错误、原因和修复对照表

| 阶段 | 关键错误或现象 | 根因 | 修复 |
|---|---|---|---|
| 原 hybrid 冷启动 | `D` 状态、`p9_client_rpc`、GPU 0 MiB | venv 位于 `/mnt/c`，大量小文件跨 WSL 边界访问 | 迁移到 `/root/.venvs/mineru` |
| EngineCore 首次 profile | `PermissionError: ptxas-blackwell` | Windows tar 丢失内部 CUDA 工具执行权限 | 恢复 Triton、tokenspeed_triton、Torch 内部 `bin` 的 `0755` |
| FlashInfer JIT | `Could not find nvcc` | 只有 CUDA runtime，没有编译器；`/usr/local/cuda` 不存在 | 安装并固定 `nvidia-cuda-nvcc==13.0.88` |
| FlashInfer JIT | `No such file or directory: ninja` | 包已安装，但 venv `bin` 未加入子进程 PATH | 包装器加入 `/root/.venvs/mineru/bin` |
| nvcc 编译 | `fatal error: nv/target` | 缺少 CCCL 头文件 | 安装 `nvidia-cuda-cccl==13.0.85` |
| 本地链接 | `cannot find -lcudart` | pip Toolkit 只有 `lib/` 和版本化 `.so`，FlashInfer 查找 `lib64/` | 建立 `lib64 -> lib` 和未版本化库链接 |
| 本地链接 | `cannot find -lcuda` | pip Toolkit 不包含 WSL 驱动 stub | 链接 `lib64/stubs/libcuda.so` 到 `/usr/lib/wsl/lib/libcuda.so` |
| Bundle 验证 | `bundle-name-source-mismatch` | 测试输出目录使用缩写名称 | 用完整源文件 stem 重新打包 |
| Bundle provenance | `conversion.backend: null` | 复用 MinerU 输出时旧逻辑主动清空设置 | 增加 `--record-conversion-settings`，仅在已知原设置时记录 |

### 14.7 推荐的 hybrid 预检顺序

今后不要直接用完整工程 PDF 检查环境。按以下顺序执行，可以更快定位：

1. 依赖完整性：

   ```bash
   /root/.venvs/mineru/bin/python -m pip check
   ```

2. Python、vLLM 和 CUDA runtime：

   ```bash
   /root/.venvs/mineru/bin/python - <<'PY'
   import torch
   import vllm
   print(torch.cuda.is_available())
   print(torch.cuda.get_device_name(0))
   print(torch.ones(1, device='cuda').item())
   PY
   ```

3. 工具存在且可执行：

   ```bash
   CUDA_HOME=/root/.venvs/mineru/lib/python3.12/site-packages/nvidia/cu13
   "$CUDA_HOME/bin/nvcc" --version
   /root/.venvs/mineru/lib/python3.12/site-packages/triton/backends/nvidia/bin/ptxas-blackwell --version
   /root/.venvs/mineru/bin/ninja --version
   ```

4. 关键头文件与库：

   ```bash
   test -f "$CUDA_HOME/include/cccl/nv/target"
   test -e "$CUDA_HOME/lib64/libcudart.so"
   test -e "$CUDA_HOME/lib64/stubs/libcuda.so"
   ```

5. 如果 FlashInfer 已生成 `build.ninja`，先单独运行 ninja，避免反复加载模型后才发现编译错误。
6. 使用 1 页、非敏感 PDF 运行 hybrid-engine。
7. 验证 manifest 中：

   ```text
   schema_version = 2.0
   conversion.backend = hybrid-engine
   source.parsed_pages = 1
   quality.status = pass
   ```

8. 最后再扩大到多页或完整工程文档。

### 14.8 哪些警告目前不是阻断问题

以下警告在本次成功运行中仍然出现，但没有导致任务失败：

- Transformers v4 将在未来 vLLM 版本中弃用。
- WSL 检测后使用 `spawn` 多进程。
- WSL 使用 `pin_memory=False`，性能可能降低。
- 首次推理出现 Triton kernel JIT latency spike。
- 关闭时提示未显式调用 `destroy_process_group()`。

它们应记录并在未来升级中处理，但不应与本次真正的阻断错误混在一起。

## 15. 最重要的工程原则

1. 运行报告必须经过当前环境复核。
2. MinerU 与 Hermes 使用独立依赖环境。
3. vLLM、Torch 和模型放在 WSL 原生文件系统。
4. `/mnt/c` 适合交换文档和代码，不适合承载重型 Python venv。
5. 转换运行时使用本地模型，不依赖外部网络。
6. pipeline 和 hybrid 分别验证，不能相互替代。
7. 使用小范围、可重复的冒烟测试逐层推进。
8. Bundle 验证通过后再进入 Hermes 受控摄取。
