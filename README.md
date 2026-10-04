# local-imagegen

一个 **DSH Skill**：用本机 GPU 上的 ComfyUI + Z-Image Turbo 离线生成图片 —— 零 API 费用、无限次试错，也支持对已生成图片做局部重绘（inpainting）。

> 这个仓库只有**前端**（技能说明 + 脚本）。出图能力来自你自己的 ComfyUI 后端与本地模型。

## 特性

- **零边际成本**：本地推理，不消耗任何云端积分
- **能力自检**：出图前先探测显卡显存、驱动、CUDA、ComfyUI 安装与模型完整性，并强制向用户确认后才启用重负载 GPU 任务
- **文生图 CLI**：常用比例预设、自定义尺寸、固定 seed 复现、批量出图挑选
- **局部重绘 CLI**：矩形或阶梯形多边形蒙版，只改一小块而不整张重生成
- **可搬运**：脚本内部没有写死的用户路径，`LOCAL_IMAGEGEN_ROOT` 等环境变量即可换机器

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 显卡 | NVIDIA，显存 ≥ 6 GB（推荐 ≥ 8 GB） |
| 驱动 | 支持 CUDA 12.8（实测 577.05 可用；580+ 可上 cu130 加速内核） |
| Python | 3.10+（脚本只用标准库；`inpaint.py` 另需 Pillow） |
| ComfyUI | 后端服务，默认路径 `D:\dsh\ComfyUI`，可用环境变量覆盖 |
| 模型 | Z-Image Turbo int8 + Qwen3-4B 文本编码器 + VAE，合计约 11.3 GB |

后端怎么搭见 [`local-imagegen/SETUP-comfyui.md`](local-imagegen/SETUP-comfyui.md)。

## 安装

1. 把 `local-imagegen/` 整个目录放进 DSH 的技能根目录（默认 `~/.agents/skills/`）：

   ```powershell
   $dest = "$env:USERPROFILE\.agents\skills"
   New-Item -ItemType Directory -Force -Path $dest | Out-Null
   Copy-Item -Path .\local-imagegen -Destination $dest -Recurse -Force
   ```

2. **确认技能加载器已启用**（最常见的坑）：DSH 需要启用本地技能提供方
   `@deepseek-ai/dsh-skill-filesystem`，否则技能目录永远是空的，agent 报
   `skill "local-imagegen" is unknown or no longer available` —— 文件都在，只是没人来读。
   完整排查步骤见 [`local-imagegen/INSTALL.md`](local-imagegen/INSTALL.md) 的 2.1 节。

3. 验证：

   ```powershell
   python local-imagegen\scripts\check_capability.py --json
   ```

## 使用

技能注册后，直接对 agent 说"用 local-imagegen 画……"即可。也可以直接调脚本：

```powershell
# 提示词写进 UTF-8 文件再传（Windows 命令行直传中文会乱码）
python local-imagegen\scripts\generate.py --prompt-file prompt.txt --ratio 16:9
python local-imagegen\scripts\generate.py --prompt-file prompt.txt --width 1344 --height 768 --seed 12345

# 局部重绘：蒙版越贴合目标物体越好
python local-imagegen\scripts\inpaint.py --image out.png --poly "1102,200 1126,200 1126,178 1246,178 1246,262 1126,262 1126,243" --prompt-file fix.txt
```

环境变量（都有默认值，换机器时覆盖即可）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_IMAGEGEN_ROOT` | `D:\dsh\ComfyUI` | ComfyUI 安装目录 |
| `LOCAL_IMAGEGEN_HOST` | `http://127.0.0.1:8188` | 服务地址 |
| `LOCAL_IMAGEGEN_UNET` / `_CLIP` / `_VAE` | 见 INSTALL.md | 模型文件名 |

## 已知边界（请勿过度期待）

- **CFG=1 让负面提示词数学上失效**：Z-Image Turbo 是蒸馏模型，不要写"不要 XX"，只做正向描述。
- **细粒度机械结构不可靠，且失败是静默的**：类别正确性高（是坦克），个体正确性低（是哪一型坦克）。
- **不适合**：技术插图、真实人物/地点还原、画面内文字、精确数量与空间关系。
- 需要事实准确性时，正路是参考图 + 图生图/ControlNet，或挂对应 LoRA。

## 目录结构

```
local-imagegen/
├── SKILL.md                     技能说明：执行流程、边界、环境坑（agent 读这个）
├── INSTALL.md                   安装与排查
├── SETUP-comfyui.md             后端搭建指南
└── scripts/
    ├── check_capability.py      出图前的硬件能力检测（必须先跑）
    ├── ensure_server.ps1        探测/拉起 ComfyUI 服务
    ├── comfy_client.py          ComfyUI 客户端 + 工作流构建
    ├── generate.py              文生图 CLI
    └── inpaint.py               局部重绘 CLI
```

## 许可

[MIT](LICENSE)

## 致谢

Z-Image Turbo（扩散模型）、Qwen3-4B（文本编码器）、[ComfyUI](https://github.com/comfyanonymous/ComfyUI)。
