# local-imagegen 安装说明

一个 DSH Skill：用本机 GPU 上的 ComfyUI + Z-Image Turbo 离线生图，零 API 费用。

## 1. 先决条件

这个 Skill 只是**前端**，它需要背后有一个跑着的 ComfyUI 服务。
如果目标机器还没有，先按 [SETUP-comfyui.md](SETUP-comfyui.md) 把后端搭起来。

| 项目 | 要求 |
| --- | --- |
| 显卡 | NVIDIA，显存 ≥ 6 GB（推荐 ≥ 8 GB） |
| 驱动 | 支持 CUDA 12.8（实测 577.05 可用；580+ 可上 cu130 提速） |
| Python | 3.10+（脚本只用标准库；`inpaint.py` 另需 Pillow） |
| ComfyUI | 默认路径 `D:\dsh\ComfyUI`，可用环境变量覆盖（见第 3 节） |
| DSH | 必须已启用本地技能加载器 `@deepseek-ai/dsh-skill-filesystem`，见 **2.1**（最常见的坑） |

## 2. 安装

把 `local-imagegen` 整个目录放到 DSH 的技能目录下：

```powershell
# Windows
$dest = "$env:USERPROFILE\.agents\skills"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path .\local-imagegen -Destination $dest -Recurse -Force
```

```bash
# macOS / Linux
mkdir -p ~/.agents/skills
cp -r ./local-imagegen ~/.agents/skills/
```

放好文件只是第一步，下面两小节决定它**能不能真的被用起来**。

### 2.1 确认目标机的技能加载器已启用（最常见的坑）

技能文件放对位置**还不够**：DSH 需要启用本地技能提供方 `@deepseek-ai/dsh-skill-filesystem`
（插件条目 id 通常是 `include:skill-filesystem`，由它负责扫描技能根目录、把技能注册进会话技能目录）。
**该插件未启用时技能目录永远是空的，症状是 agent 调用时报
`skill "local-imagegen" is unknown or no longer available`** —— 文件都在，就是没人来读。

自查：

```powershell
# 1) 文件在不在、层级对不对（目录规则见 2.2）
Get-ChildItem "$env:USERPROFILE\.agents\skills\local-imagegen" -Recurse -File

# 2) 让 agent 用 skill 工具按名加载 local-imagegen；报 unknown 就是加载器没起来
```

确认加载器未启用时，让 agent 启用它（`plugin_manager` 的 `set_plugin`，target 填
`include:skill-filesystem`，`enabled: true`）。启用后该条目状态会从 `inactive` 变为
`schema`/`active`，技能**立即生效、无需重启**。

### 2.2 目录规则（不满足会被静默忽略）

- 技能必须是**技能根目录下的一层**：`<root>/local-imagegen/SKILL.md`，或扁平文件 `<root>/local-imagegen.md`。
  **嵌套更深的 `**/SKILL.md` 不会被扫描**。
- 目录名与 `SKILL.md` frontmatter 的 `name` 必须是 **kebab-case**，且 `description` 必填；
  校验不过的技能会被静默跳过 —— 在 agent 端看起来就是"技能不存在"，没有额外报错。
- 默认技能根及优先级：`<projectRoot>/.dsh/skills` → `<projectRoot>/.agents/skills` →
  `$DSH_HOME/skills`（默认 `~/.dsh/skills`）→ `~/.agents/skills`。
  同名技能按此顺序取优先级更高的一份。

## 3. 配置路径（可选）

默认值是打包机器上的实测路径（`D:\dsh\ComfyUI`）。**换机器用环境变量覆盖，不必改代码** ——
技能脚本内部除这个可覆盖的默认值外，没有任何写死的用户路径，可以整体搬到任意目录：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_IMAGEGEN_ROOT` | `D:\dsh\ComfyUI` | ComfyUI 安装目录 |
| `LOCAL_IMAGEGEN_HOST` | `http://127.0.0.1:8188` | 服务地址 |
| `LOCAL_IMAGEGEN_UNET` | `z_image_turbo_int8_convrot.safetensors` | 扩散模型文件名 |
| `LOCAL_IMAGEGEN_CLIP` | `qwen_3_4b_fp8_mixed.safetensors` | 文本编码器文件名 |
| `LOCAL_IMAGEGEN_VAE` | `ae.safetensors` | VAE 文件名 |

## 4. 验证

```powershell
python scripts\check_capability.py
```

期望看到显卡型号、显存、CUDA 可用性、ComfyUI 安装状态、三个模型文件的校验结果，
以及一句 `需向用户确认：……是否启用本地生图？`。

`eligible: false` 说明本机跑不了，脚本会说明具体原因。

## 5. 目录结构

```
local-imagegen/
├── SKILL.md                     技能说明：执行流程、边界、环境坑（Agent 读这个）
├── INSTALL.md                   本文件
├── SETUP-comfyui.md             后端搭建指南
└── scripts/
    ├── check_capability.py      出图前的硬件能力检测（必须先跑）
    ├── ensure_server.ps1        探测/拉起 ComfyUI 服务
    ├── comfy_client.py          ComfyUI 客户端 + 工作流构建（被下面两个 import）
    ├── generate.py              文生图 CLI
    └── inpaint.py               局部重绘 CLI
```

## 6. 注意事项

- **没有 `pwsh`（PowerShell 7）的机器上不要写 `pwsh -File`**，直接 `& '路径\ensure_server.ps1'` 调用。
  该脚本用的是 Windows PowerShell 5.1 兼容语法。
- 中文提示词**写进 UTF-8 文件再用 `--prompt-file` 传入**。直接走命令行参数在 Windows 上会遇到编码问题。
- 脚本入口已强制 `stdout` 为 UTF-8；在非 Windows 平台上该调用是空操作。
- 出图分辨率不要超过 `check_capability.py` 报的 `maxPixels`。
- **换机器后第一件事就是跑 `check_capability.py`**：显卡、驱动、CUDA、ComfyUI 安装、三个模型文件
  它都会核对。`status: needs_setup` 说明后端还没搭（见 [SETUP-comfyui.md](SETUP-comfyui.md)）；
  `eligible: false` 说明这台机器跑不了，别硬上。
- 这个分发包**只有前端**：不含 ComfyUI、不含模型（三个模型合计约 11.3 GB）。对方必须自己按
  SETUP-comfyui.md 搭好后端，否则技能只能检测出 `needs_setup`，无法出图。
