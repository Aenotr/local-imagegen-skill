# dsh-image-modes

把 **云端 / 本地 / 混合** 三种出图方式整合成 **一个工具** 的 DSH 插件。

```
gen_image(mode = "cloud" | "local" | "hybrid", prompt, ...)
image_modes_status()          # 开工前自检：三种方式各自能不能用
```

## 为什么会有这个插件

云端与本地原本是两套毫不相干的安装（一个第三方插件 + 一个技能），而"混合"只是 agent 每次临场拼的 shell 流水线。这个插件把三者变成**同一条确定性的代码路径**，并把实测踩出来的约束固化进去：

| 方式 | 实测特性 |
| --- | --- |
| `cloud` | 最高 4K 档（2K ≈ 3.9MP）、一次到位、**看得懂画面**；但**没有蒙版参数、没有 seed**，每次都整幅重绘 → 角色神态会漂 |
| `local` | ComfyUI + Z-Image Turbo，12–32 秒/张、**零成本**、**可固定 seed 复现**、**像素级蒙版重绘**；上限约 1.03MP（8GB 显存） |
| `hybrid` | 先本地出草稿（或本地打底），再交云端高清重构。实测**唯一**同时保住"完成度 + 参考角色神态"的路线；代价是两阶段 |

插件里还写死了两条用真金白银换来的构图规则：

- **全身立绘用 `ratio: "9:16"`** —— 3:4 画幅会把脚裁掉（连续两张都被裁）
- **本地区部重绘时蒙版必须贴合目标物体** —— 用覆盖背景的大框，模型会把框内背景整片重刷成白色

## 安装

### 方式一：从 GitHub 装（推荐）

在 DSH 里让 agent 执行（`plugin_manager` 的 `install_bundle`）：

```
install_bundle  target = github:<你的用户名>/dsh-image-modes
```

### 方式二：手动放进 profile

1. 把整个 `dsh-image-modes` 目录放到 `<profile>/vendor/dsh-image-modes`
2. 编辑 `<profile>/package.json`：

```json
{
  "dependencies": {
    "dsh-image-modes": "file:./vendor/dsh-image-modes"
  },
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "dsh-image-modes"]
    }
  }
}
```

3. 在 profile 目录执行 `pnpm install`，然后重载 profile

装好后两个工具会出现在 agent 的工具表里，**无需重启**（live profile 会热重组）。

## 配置

配置优先级：**本行 `config:` → 环境变量 → 内置默认值**。要覆盖，就在自己 profile 的 `cordis.patch.yml` 里按 `id: dsh-image-modes` 重述整个 config。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `apiBase` | `https://api.agnes-ai.cn` | 云端图像 API 根（OpenAI 兼容） |
| `apiKeyRef` | `IMAGE_API_KEY` | 凭据域名里存放密钥的引用名 |
| `baseUrlRef` | `IMAGE_API_BASE` | 同上，存放 API 根 |
| `imageModel` / `visionModel` | `agnes-image-2.1-flash` / `agnes-2.5-flash` | 生图 / 识图模型 |
| `comfyHost` | `http://127.0.0.1:8188` | 本地 ComfyUI 地址 |
| `comfyRoot` | `D:\dsh\ComfyUI` | ComfyUI 安装目录（读产出文件用） |
| `unet` / `clip` / `vae` | Z-Image Turbo / Qwen3-4B / ae | 本地模型文件名 |
| `ensureServerScript` | 空 | ComfyUI 没起来时用它拉起（填技能里的 `ensure_server.ps1` 绝对路径） |
| `outputDir` | `generated_images` | 产出目录（相对会话工作目录） |

**密钥不需要重复配置**：本插件与 `dsh-imagegen` 共用 `IMAGE_API_KEY` / `IMAGE_API_BASE` 这套引用，profile 里配过一次就两边都能用。若未配置，`cloud` / `hybrid` 会明确报错，`local` 不受影响。

本地侧的环境变量也与 `local-imagegen` 技能保持一致：`LOCAL_IMAGEGEN_HOST` / `LOCAL_IMAGEGEN_ROOT` / `LOCAL_IMAGEGEN_UNET` / `LOCAL_IMAGEGEN_CLIP` / `LOCAL_IMAGEGEN_VAE`。

## 用法

```
image_modes_status()                                  # 先看三种方式各自可用性

gen_image(mode="local",  prompt="...", count=4, ratio="9:16")
gen_image(mode="cloud",  prompt="...", size="2K", ratio="9:16")
gen_image(mode="hybrid", prompt="...", count=4, ratio="9:16")
gen_image(mode="local",  prompt="把外套改成深墨绿", images=["base.png"], mask="mask.png", denoise=0.7)
```

`gen_image` 的**工具描述里就写着"调用前必须先问用户用哪种模式并说明取舍"** —— 规则固化在代码里，不依赖 agent 临场记性。

## 依赖与边界

- **`local` / `hybrid` 需要**：本机有 ComfyUI 与三个模型文件（合计约 11.3GB），NVIDIA 显卡显存 ≥6GB。没有后端时只有 `cloud` 可用。
- **`cloud` / `hybrid` 需要**：一个 OpenAI 兼容的图像 API 密钥。
- **本地侧不依赖 Python**：工作流走 ComfyUI 的 HTTP 接口，节点图与 `local-imagegen` 技能完全一致。
- **`hybrid` 目前不含"裁剪→本地重绘→贴回"的像素级贴回**（那一步需要图像合成）。需要像素级锁定时，用 `local` + 紧贴蒙版，或配合 `local-imagegen` 技能的 `hybrid_patch.py`。
- 云端**没有蒙版**是服务端能力缺失（请求体只接受 model/prompt/size/ratio/image），插件无法绕过。

## 许可

MIT
