# ComfyUI + Z-Image Turbo 后端搭建指南

实测环境：Windows 10/11 + RTX 5060 Laptop 8 GB + 驱动 577.05 + Python 3.12。
其他 NVIDIA 显卡同样适用，按下表调整量化档位即可。

## 关键决策（先看这个，能省几小时）

### 1. PyTorch 用 cu128，不要用 cu130

驱动 577.05 对应 CUDA 12.9，**CUDA 13 需要 580+ 驱动**。所以只能装 cu128：

```powershell
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```

代价：ComfyUI 的 `comfy_kitchen` CUDA 加速后端要求 cu130，会被禁用，回落到 eager 路径。
出图仍可用（实测 1344×768 / 8 步约 30–40 秒），但**不是最快**。升级驱动到 580+ 后可换 cu130 提速。

### 2. 模型用 int8 量化档，不要用 bf16

Z-Image Turbo 是 5.7B 参数模型，官方仓库是 fp32 分片（transformer 23.4 GB）。
ComfyUI 官方重打包版提供三档：

| 档位 | DiT 体积 | 8 GB 显存能否放下 |
| --- | --- | --- |
| `z_image_turbo_bf16` | 11.46 GB | ❌ 放不下，被迫反复换出 |
| **`z_image_turbo_int8_convrot`** | **5.78 GB** | ✅ **推荐** |
| `z_image_turbo_nvfp4` | 4.20 GB | ✅ 更小，但需 Blackwell + 内核支持 |

文本编码器同理：`qwen_3_4b_fp8_mixed`（5.25 GB）优于 `qwen_3_4b`（7.49 GB）。

### 3. 采样参数是固定的

Z-Image Turbo 是蒸馏模型，必须按官方模板取值：

```
采样器 res_multistep | 调度器 simple | 步数 8 | CFG 1.0 | ModelSamplingAuraFlow shift=3
```

**CFG=1 意味着负面提示词在数学上完全失效**（无条件分支被约掉）。
不要写"不要 XX"，只做正向描述。这一点在 SKILL.md 里有详细说明。

## 国内网络可用源

| 用途 | 可用 | 不可用 |
| --- | --- | --- |
| GitHub 源码 | `ghproxy.net`、`gh-proxy.com`（前缀代理） | github.com、codeload.github.com |
| 模型 | `modelscope.cn`（约 10–20 MB/s）、`hf-mirror.com` | huggingface.co |
| PyPI | `mirrors.aliyun.com/pypi/simple/` | — |
| PyTorch | `download.pytorch.org`（可达） | — |

`Comfy-Org/z_image_turbo` 在 **ModelScope 上有同名镜像**，比 hf-mirror 快约 3 倍，优先用它。

## 搭建步骤

### 1. Python 3.12

用系统 Python 或任何 3.12 发行版。注意 Windows 自带的 `python.exe` 在
`WindowsApps` 下可能只是 Microsoft Store 占位符（运行会提示"Python was not found"），需自行安装。

### 2. 取 ComfyUI 源码

```python
# github 被阻断时走 ghproxy；Range 头会被忽略，会下载完整 13 MB
url = 'https://ghproxy.net/https://github.com/comfyanonymous/ComfyUI/archive/refs/heads/master.zip'
```

解压**用 Python 的 `zipfile`，不要用 PowerShell 的 `Expand-Archive`**——后者在含空目录的归档上会报
`PathNotFound` 并中断。

### 3. 建虚拟环境

```powershell
python -m venv ComfyUI\venv
```

**两个已知失败点：**

- **`ensurepip` 报 `PermissionError`**：它要往 `%TEMP%` 写引导 wheel。先把 `TEMP`/`TMP` 指到一个
  可写目录再建 venv。
- 若 `ensurepip` 仍失败，用 `--without-pip` 建 venv，再把 Python 自带的
  `Lib\ensurepip\_bundled\pip-*.whl` **当普通 zip 解压**进 `venv\Lib\site-packages` 即可。
  pip 有 `__main__.py`，解压后 `python -m pip` 就能用。
- **不要 `pip install --upgrade pip`**：Windows 上无法覆盖正在运行的 pip 自身文件，会报
  `Errno 13 Permission denied`。用自带的 pip 就行。

### 4. 安装依赖

```powershell
$env:PIP_CACHE_DIR = '<工作区>\.cache\pip'
$env:TMP = '<工作区>\.tmp'; $env:TEMP = '<工作区>\.tmp'

venv\Scripts\python.exe -m pip install torch torchvision torchaudio `
    --index-url https://download.pytorch.org/whl/cu128

venv\Scripts\python.exe -m pip install -r ComfyUI\requirements.txt `
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

验证：

```powershell
venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_capability(0))"
```

期望：`2.11.0+cu128 12.8 True (12, 0)`（最后一项是 sm_120 = Blackwell）。

### 5. 下载模型

从 ModelScope 取 `Comfy-Org/z_image_turbo`，按上表选档位。三个文件放到：

```
ComfyUI\models\diffusion_models\z_image_turbo_int8_convrot.safetensors
ComfyUI\models\text_encoders\qwen_3_4b_fp8_mixed.safetensors
ComfyUI\models\vae\ae.safetensors
```

ModelScope 文件接口（`Sha256` 字段可用于校验，**务必校验**）：

```
https://www.modelscope.cn/api/v1/models/Comfy-Org/z_image_turbo/repo/files?Revision=master&Recursive=True
https://www.modelscope.cn/api/v1/models/Comfy-Org/z_image_turbo/repo?Revision=master&FilePath=<urlencoded 路径>
```

### 6. 启动与验证

```powershell
venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188
```

或直接用技能里的 `scripts\ensure_server.ps1`。

启动日志里应能看到 `Total VRAM 8151 MB`、`Device: cuda:0`、`Using pytorch attention`。
出现 `You need pytorch with cu130 or higher to use optimized CUDA operations` 是预期内的，
见前面"关键决策 1"。

## 已知坑速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `schannel: SEC_E_NO_CREDENTIALS` / `irm` 全部失败 | 低完整性令牌下 Schannel 拿不到凭据 | 用 Python 的 urllib/requests 下载（走 OpenSSL） |
| `pip` 报 `Errno 13` 写临时目录失败 | 沙箱拒绝写入 `tempfile.mkdtemp` 新建目录 | 安装类操作需临时提权；运行期不受影响 |
| `Expand-Archive` 抛 `PathNotFound` | PowerShell 处理空目录的缺陷 | 改用 Python `zipfile` |
| `pip install --upgrade pip` 报权限拒绝 | Windows 无法覆盖运行中的 pip | 跳过升级 |
| 出图极慢、显存爆 | 用了 bf16 全量模型 | 换 int8 档 |
| 中文变乱码 | Python 在 Windows 管道下用 ANSI 代码页 | 入口脚本加 `sys.stdout.reconfigure(encoding='utf-8')` |

## 实测性能（RTX 5060 Laptop 8 GB，int8 档，cu128 eager 路径）

| 分辨率 | 耗时 |
| --- | --- |
| 768×768 | 约 16 秒 |
| 1024×1024 | 约 32 秒 |
| 1344×768（16:9） | 约 30–40 秒 |
| 896×1152（3:4） | 约 30 秒 |

首次出图会比后续慢几秒（模型加载）。系统内存偏小时（实测 16 GB）不要同时跑多个吃内存的任务。
