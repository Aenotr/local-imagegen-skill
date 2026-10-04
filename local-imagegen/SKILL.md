---
name: local-imagegen
description: 用本机 GPU 上的 ComfyUI + Z-Image Turbo 离线生成图片，零 API 费用、无内容审查、无限次试错。当用户要求生成图片、想批量出图挑选、需要明确不消耗积分或不走云端、或提到 local-imagegen / 本地生图 / ComfyUI 时使用。也用于对已生成图片做局部重绘（inpainting）修补。启用前必须先检测本机显卡能力（显存、驱动、CUDA），并以「本地生图需要高性能显卡，是否启用」向用户确认后再动手。需要事实准确性、特定型号机械细节或画面内文字的场景不适用，改用带参考图或 LoRA 的方案。
---

# 本地生图（ComfyUI + Z-Image Turbo）

在本机 GPU 上离线出图。边际成本为零，可无限次重试，适合探索构图、氛围和风格。

## 第零步：能力检测与用户确认（必做，不可跳过）

本地生图是重负载 GPU 任务，**在出图或启动 ComfyUI 之前，先跑能力检测**：

```powershell
# <skill> = 本技能的基目录，DSH 加载技能时会直接给出（默认 ~/.agents/skills/local-imagegen）
# 下文所有 <skill>\... 都替换成它。脚本内部不含任何写死的用户路径，可整体搬到任意目录
& '<skill>\scripts\check_capability.py' --json
```

用 `load_workspace_dependencies` 返回的 Python 执行。返回字段：

| 字段 | 含义 |
| --- | --- |
| `status` | `ready` / `ready_degraded` / `needs_setup` / `unsupported` |
| `tier` | `comfortable` / `ok` / `tight` / `insufficient` / `none` |
| `eligible` | 本机**技术上**能否运行本地生图 |
| `requiresUserConsent` | 恒为 `true` |
| `enablePrompt` | **必须转述给用户的问题原文** |
| `maxPixels` | 本机建议的最大像素数 |
| `reasons` | 判定依据，一并告知用户 |

然后按下面的规则处理：

1. **把 `enablePrompt` 作为问题问用户，等到明确答复再继续。** 用 `ask_user_question`，不得自问自答、不得默认同意。`requiresUserConsent` 的意义就是禁止 Agent 擅自开启重负载 GPU 任务。
2. `eligible: false` 或 `status: unsupported` —— **不要尝试启用，也不要自作主张去"修好"它**（除非用户明确要求安装）。向用户说明原因，并建议改用云端生图方案。
3. `status: needs_setup` —— 先问用户是否现在安装 ComfyUI 与模型，得到同意再动手。
4. `tier: tight` —— 明确告知"能跑但慢、分辨率受限"，让用户知情后决定。
5. 同一会话内用户已确认、且环境未变化时不必重复询问；用户改变主意或环境变化时重新检测。
6. **出图分辨率不得超过 `maxPixels`**，不要为追求观感擅自超配——超配会触发显存换页，速度骤降甚至失败。

用户拒绝启用时：停止本地生图相关操作，**不要偷偷启动 ComfyUI**。

## 前置条件

- ComfyUI 安装目录：默认 `D:\dsh\ComfyUI`，**换机器时用环境变量 `LOCAL_IMAGEGEN_ROOT` 覆盖**（脚本里没有其它路径假设）
- 模型已就位：`models\diffusion_models\z_image_turbo_int8_convrot.safetensors`、
  `models\text_encoders\qwen_3_4b_fp8_mixed.safetensors`、`models\vae\ae.safetensors`
- 参考配置（本技能实测通过的环境）：RTX 5060 Laptop 8GB + int8 量化模型。**别换 bf16 全量版**（11.46GB，8G 卡放不下）

以上都由 `check_capability.py` 自动核对，不需要手工检查。

## 第一步：确认服务在跑

直接用 shell 工具执行脚本。该脚本按 Windows PowerShell 5.1 兼容语法编写，**不要写 `pwsh -File`**（目标机没有 PS7 时会直接失败）：

```powershell
& '<skill>\scripts\ensure_server.ps1'
```

服务未启动时该脚本会拉起 ComfyUI 并等到就绪。**如果出图期间还要做别的事，用后台任务方式（`run_in_background`）启动**，否则它只保证在脚本执行期间存活。

`ensure_server.ps1` 用 `Start-Process` 拉起的 ComfyUI 是独立进程，脚本退出后仍存活；但它不随 DSH 会话自动重启，机器重启或进程被杀后需重新执行本脚本。

不要用 `Start-Process` 从别处随手拉起，也不要重复启动第二个实例——端口 8188 已被占用时新实例会失败。

## 出图

```powershell
# 提示词写进 UTF-8 文件再传：中文直接走命令行参数会遇到编码问题
python <skill>\scripts\generate.py --prompt-file prompt.txt --ratio 16:9
python <skill>\scripts\generate.py --prompt-file prompt.txt --ratio 3:4 --count 4
python <skill>\scripts\generate.py --prompt-file prompt.txt --width 1344 --height 768 --seed 12345
```

用 `load_workspace_dependencies` 返回的 Python 执行（脚本只用标准库）。输出是一行一个 PNG 绝对路径。

| 参数 | 说明 |
| --- | --- |
| `--ratio` | `1:1` `4:3` `3:4` `3:2` `2:3` `16:9` `9:16` `21:9`，预设尺寸自动取 |
| `--width/--height` | 自定义尺寸，**必须是 16 的倍数** |
| `--count` | 出几张，每张独立随机 seed，用于挑选而非堆在同一个节点里 |
| `--seed` | 固定 seed 可复现同一张图 |
| `--steps` | 默认 8，够用；调高收益很小 |
| `--json` | 机器可读输出 |

尺寸参考：`1344x768` 出图约 30–40 秒，`896x1152` 相近，`1536x640` 稍慢。**分辨率上限以 `check_capability.py` 报的 `maxPixels` 为准**（8G 显存实测 1.03M 像素），超配会触发显存换页、速度骤降。

提示词用中文或英文都可以，Qwen3-4B 文本编码器对中文支持好。**描述具体、包含材质与光线信息**的提示词效果明显更好。

## 局部重绘

已经出好的图只改一小块时用它，不要整张重生成：

```powershell
python <skill>\scripts\inpaint.py --image out.png --rect 1114,172,1250,268 --prompt-file fix.txt
python <skill>\scripts\inpaint.py --image out.png --poly "1102,200 1126,200 1126,178 1246,178 1246,262 1126,262 1126,243" --prompt-file fix.txt
```

**蒙版形状是成功的关键**：蒙版越贴合目标物体越好。蒙版里如果暴露了本该是背景的区域，模型必须凭空编造那块背景，结果通常是灰色涂抹和接缝环。贴合物体的"台阶形"多边形远好于大方块。

## 交付

生成后**必须实际查看图片内容再交付**，不能只看进程退出码。用 `read_image` 打开，确认符合用户意图后再回复。发现问题优先局部重绘，而不是无脑重出。

多条产物用 `present` 给出，或在回复里用 `![描述](绝对路径)` 内嵌。

## 已知边界（重要，别过度承诺）

1. **CFG=1 让负面提示词在数学上完全失效。** Z-Image Turbo 是蒸馏模型，必须在 CFG=1 下运行，此时无条件分支被约掉，负面提示词不参与计算。**不要写"不要 XX"**，只做正向描述。
2. **细粒度机械结构不可靠，且失败是静默的。** 实测：画 T-34/85 时反复生成炮口制退器（实车没有），显式否定、换 seed、蒙版重绘、让炮管出画都压不住。模型输出的是训练数据的统计平均，没有查证事实的机制。**类别正确性高（是坦克），个体正确性低（是哪一型坦克）。**
3. **不适合**：技术插图、真实人物/地点还原、画面内文字、精确数量与空间关系。
4. **不能自己判断对错。** 图的观感始终自信，错了也不会提示。面对用户不熟悉的领域，主动说明这一限制，别把生成图当资料。

需要真正的准确性时，正路是**参考图 + 图生图/ControlNet**，或挂该型号的 LoRA，而不是继续调提示词。

## 环境坑（本机实测记录，换机器可能不同）

- **pip / 临时目录**：沙箱拒绝写入工作区内新建的 `tempfile.mkdtemp` 目录，所以任何 pip 安装都需要临时提权。运行期出图不受影响。
- **HTTPS**：低完整性沙箱下 Schannel 走不通（`curl`/`Invoke-WebRequest` 全失败）。Python 的 OpenSSL 正常，下载用 Python。本机可达的源：ModelScope（模型，约 10–20MB/s）、`hf-mirror.com`、`ghproxy.net`（GitHub 代理）、阿里云 PyPI、`download.pytorch.org`。**github.com 和 huggingface.co 被阻断。**
- **加速内核未启用**：驱动 577.05（CUDA 12.9），而 ComfyUI 的 `comfy_kitchen` CUDA 后端要求 cu130（需 580+ 驱动），当前回落到 eager 路径。升级驱动后换装 cu130 版 PyTorch 可提速。
- **系统内存只有 16GB**，ComfyUI 已启用 pinned memory 与动态卸载。不要同时跑多个吃内存的任务。
