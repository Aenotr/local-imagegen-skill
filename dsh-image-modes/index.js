// dsh-image-modes — put the three ways of making images behind ONE tool.
//
//   gen_image(mode: 'cloud' | 'local' | 'hybrid', prompt, ...)
//
// Why this exists: cloud (`dsh-imagegen`) and local (`local-imagegen` skill) are
// two unrelated installs, and "hybrid" was only ever an agent improvising a shell
// pipeline. This plugin makes all three a single deterministic code path and bakes
// in the constraints that were measured the hard way:
//
//   · cloud  — no mask support and no seed; it always repaints the whole frame,
//              so it wins on "understand the picture" edits and loses on identity.
//   · local  — ComfyUI + Z-Image Turbo, 12–32s per image, free, reproducible seed,
//              pixel-locked inpaint; capped at ~1.03 MP (8 GB card).
//   · hybrid — local drafts (or a local base) first, then a cloud hi-res refactor.
//              Measured: this is the only route that kept both completion and the
//              reference character's signature expression.
//
// It also encodes two composition rules that cost real attempts to learn: a
// full-body figure needs a 9:16 frame or the feet get cropped, and a local repaint
// needs a mask that hugs the object (a big box makes the model repaint the
// background inside it).
//
// Configuration precedence (first match wins):
//   1. this row's `config:` in the profile (id-targeted override works)
//   2. environment  — IMAGE_API_KEY / IMAGE_API_BASE, LOCAL_IMAGEGEN_ROOT / HOST /
//                     UNET / CLIP / VAE (shared with the local-imagegen skill)
//   3. built-in defaults
// The API key is resolved through the credentials domain when available, so a
// profile that already configured `dsh-imagegen` needs no second setup.
import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { dirname, extname, isAbsolute, join, resolve } from 'node:path';

export const name = 'dsh-image-modes';
export const inject = ['tools'];

const DEFAULT_API_BASE = 'https://api.agnes-ai.cn';
const DEFAULT_API_KEY_REF = 'IMAGE_API_KEY';
const DEFAULT_BASE_URL_REF = 'IMAGE_API_BASE';
const DEFAULT_IMAGE_MODEL = 'agnes-image-2.1-flash';
const DEFAULT_VISION_MODEL = 'agnes-2.5-flash';
const DEFAULT_COMFY_HOST = 'http://127.0.0.1:8188';
const DEFAULT_COMFY_ROOT = 'D:\\dsh\\ComfyUI';
const DEFAULT_UNET = 'z_image_turbo_int8_convrot.safetensors';
const DEFAULT_CLIP = 'qwen_3_4b_fp8_mixed.safetensors';
const DEFAULT_VAE = 'ae.safetensors';
const DEFAULT_STEPS = 8;
/** Local presets. Every side must be a multiple of 16 (VAE 8x + patch 2x). */
const LOCAL_RATIOS = {
  '1:1': [1024, 1024],
  '4:3': [1152, 896],
  '3:4': [896, 1152],
  '3:2': [1216, 832],
  '2:3': [832, 1216],
  '16:9': [1344, 768],
  '9:16': [768, 1344],
  '21:9': [1536, 640],
};
const CLOUD_RATIOS = ['1:1', '3:4', '4:3', '16:9', '9:16', '2:3', '3:2', '21:9'];
const CLOUD_SIZES = ['1K', '2K', '3K', '4K'];
const MIME = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' };

const TRADEOFFS = [
  'cloud  = 3.9MP one shot, understands the picture, no mask/seed, repaints everything (identity drifts).',
  'local  = free, reproducible seed, pixel-locked inpaint, <=1.03MP, cannot invent meaning.',
  'hybrid = local drafts/base first, then cloud hi-res refactor; best completion + identity, two stages.',
].join(' ');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function apply(ctx, config = {}) {
  const env = (k) => (k && process.env[k] ? process.env[k] : '');

  function normalizeBase(value) {
    return String(value || '').replace(/\/v1\/?$/i, '').replace(/\/+$/, '');
  }

  const cfg = {
    comfyHost: String(config.comfyHost || env('LOCAL_IMAGEGEN_HOST') || DEFAULT_COMFY_HOST).replace(/\/+$/, ''),
    comfyRoot: config.comfyRoot || env('LOCAL_IMAGEGEN_ROOT') || DEFAULT_COMFY_ROOT,
    unet: config.unet || env('LOCAL_IMAGEGEN_UNET') || DEFAULT_UNET,
    clip: config.clip || env('LOCAL_IMAGEGEN_CLIP') || DEFAULT_CLIP,
    vae: config.vae || env('LOCAL_IMAGEGEN_VAE') || DEFAULT_VAE,
    ensureServerScript: config.ensureServerScript || '',
    outputDir: config.outputDir || 'generated_images',
    steps: Number.isFinite(config.steps) ? config.steps : DEFAULT_STEPS,
  };

  let ensuring = null;

  // ── generic helpers ────────────────────────────────────────────────────────

  async function withRetry(label, fn, attempts = 3, baseMs = 1500) {
    let last;
    for (let i = 0; i < attempts; i += 1) {
      try {
        return await fn(i);
      } catch (e) {
        last = e;
        if (i < attempts - 1) await sleep(baseMs * (i + 1));
      }
    }
    throw new Error(`${label} failed after ${attempts} attempts: ${(last && last.message) || last}`);
  }

  function cwdOf(exec) {
    const header = exec && exec.agent && exec.agent.session && exec.agent.session.header;
    return (header && header.cwd) || process.cwd();
  }

  function resolveIn(cwd, p) {
    return isAbsolute(p) ? p : resolve(cwd, p);
  }

  async function saveUnique(wanted, buffer) {
    const ext = extname(wanted) || '.png';
    const stem = wanted.slice(0, wanted.length - ext.length);
    for (let n = 0; ; n += 1) {
      const candidate = n === 0 ? wanted : `${stem}-${n}${ext}`;
      try {
        await writeFile(candidate, buffer, { flag: 'wx' });
        return candidate;
      } catch (e) {
        if (!e || e.code !== 'EEXIST') throw e;
      }
    }
  }

  async function cloudEnv() {
    const apiKeyRef = config.apiKeyRef || DEFAULT_API_KEY_REF;
    const baseUrlRef = config.baseUrlRef || DEFAULT_BASE_URL_REF;
    let apiKey = config.apiKey || env(apiKeyRef) || env('AGNES_API_KEY') || '';
    let base = normalizeBase(config.apiBase || env(baseUrlRef) || env('AGNES_API_BASE') || DEFAULT_API_BASE);
    const credentials = ctx.get && ctx.get('credentials');
    if (credentials && typeof credentials.resolve === 'function') {
      try {
        const k = await credentials.resolve(apiKeyRef);
        if (k && k.value) apiKey = k.value;
      } catch (e) { /* fall back to config/env */ }
      try {
        const b = await credentials.resolve(baseUrlRef);
        if (b && b.value && !config.apiBase) base = normalizeBase(b.value);
      } catch (e) { /* fall back to config/env */ }
    }
    return {
      apiKey,
      base,
      imageModel: config.imageModel || DEFAULT_IMAGE_MODEL,
      visionModel: config.visionModel || DEFAULT_VISION_MODEL,
    };
  }

  async function toDataUri(input, cwd) {
    if (/^https?:\/\//i.test(input) || /^data:/i.test(input)) return input;
    const p = resolveIn(cwd, input);
    const buf = await readFile(p);
    return `data:${MIME[extname(p).toLowerCase()] || 'image/png'};base64,${buf.toString('base64')}`;
  }

  // ── cloud backend ─────────────────────────────────────────────────────────

  async function cloudGenerate({ prompt, images, size, ratio, signal }) {
    const { apiKey, base, imageModel } = await cloudEnv();
    if (!apiKey) {
      throw new Error('cloud mode needs an API key: set the IMAGE_API_KEY credential '
        + '(Settings → Plugins → 图像生成, the same one dsh-imagegen uses) or config.apiKey.');
    }
    const body = { model: imageModel, prompt, size: size || '2K' };
    if (CLOUD_SIZES.includes(body.size) && ratio) body.ratio = ratio;
    if (images && images.length) body.image = images;
    body.extra_body = { response_format: 'url' };

    const data = await withRetry('cloud image API', async () => {
      const res = await fetch(`${base}/v1/images/generations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify(body),
        signal,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 300)}`);
      }
      return res.json();
    }, 4, 2500);

    const item = Array.isArray(data && data.data) ? data.data[0] : null;
    const url = item && (item.url || (item.b64_json ? `data:image/png;base64,${item.b64_json}` : ''));
    if (!url) throw new Error('image API returned no image');
    const buffer = Buffer.from(await withRetry('download image', async () => {
      const r = await fetch(url, { signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.arrayBuffer();
    }, 3, 1500));
    return { buffer, url, model: imageModel, base };
  }

  async function visionRank(uris, prompt, signal) {
    const { apiKey, base, visionModel } = await cloudEnv();
    if (!apiKey) throw new Error('vision pick needs an API key');
    const content = uris.map((u) => ({ type: 'image_url', image_url: { url: u } }));
    content.push({ type: 'text', text: prompt });
    const data = await withRetry('vision rank', async () => {
      const res = await fetch(`${base}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
          model: visionModel,
          messages: [{ role: 'user', content }],
          temperature: 0.2,
          max_tokens: 400,
        }),
        signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }, 3, 2000);
    const text = String(data?.choices?.[0]?.message?.content || '').trim();
    const m = text.match(/(?:^|\D)(\d{1,2})(?:\D|$)/);
    const idx = m ? Number(m[1]) - 1 : 0;
    return { idx: Number.isInteger(idx) && idx >= 0 && idx < uris.length ? idx : 0, text, model: visionModel };
  }

  // ── local backend (ComfyUI over HTTP; no Python needed) ───────────────────

  async function comfyGet(path, timeoutMs = 15000) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(cfg.comfyHost + path, { signal: ac.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(t);
    }
  }

  async function comfyStats(timeoutMs = 8000) {
    try {
      return await comfyGet('/system_stats', timeoutMs);
    } catch (e) {
      return null;
    }
  }

  async function ensureLocal() {
    if (await comfyStats()) return true;
    if (ensuring) return ensuring;
    ensuring = (async () => {
      if (!cfg.ensureServerScript) {
        throw new Error(`ComfyUI is not reachable at ${cfg.comfyHost}. Start it, or set `
          + 'config.ensureServerScript to the skill\'s ensure_server.ps1 so this plugin can start it.');
      }
      try {
        const child = spawn('powershell.exe',
          ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', cfg.ensureServerScript],
          { detached: true, stdio: 'ignore', windowsHide: true });
        child.unref();
      } catch (e) {
        throw new Error(`could not launch ${cfg.ensureServerScript}: ${e.message}`);
      }
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        await sleep(4000);
        if (await comfyStats()) return true;
      }
      throw new Error('ComfyUI did not become ready within 300s');
    })().finally(() => { ensuring = null; });
    return ensuring;
  }

  function graphT2I(prompt, width, height, steps, seed) {
    return {
      1: { class_type: 'UNETLoader', inputs: { unet_name: cfg.unet, weight_dtype: 'default' } },
      2: { class_type: 'ModelSamplingAuraFlow', inputs: { model: ['1', 0], shift: 3 } },
      3: { class_type: 'CLIPLoader', inputs: { clip_name: cfg.clip, type: 'lumina2', device: 'default' } },
      4: { class_type: 'VAELoader', inputs: { vae_name: cfg.vae } },
      5: { class_type: 'CLIPTextEncode', inputs: { clip: ['3', 0], text: prompt } },
      6: { class_type: 'ConditioningZeroOut', inputs: { conditioning: ['5', 0] } },
      7: { class_type: 'EmptySD3LatentImage', inputs: { width, height, batch_size: 1 } },
      8: {
        class_type: 'KSampler',
        inputs: {
          model: ['2', 0], positive: ['5', 0], negative: ['6', 0], latent_image: ['7', 0],
          seed, steps, cfg: 1.0, sampler_name: 'res_multistep', scheduler: 'simple', denoise: 1.0,
        },
      },
      9: { class_type: 'VAEDecode', inputs: { samples: ['8', 0], vae: ['4', 0] } },
      10: { class_type: 'SaveImage', inputs: { images: ['9', 0], filename_prefix: 'dsh_image_modes' } },
    };
  }

  function graphInpaint({ prompt, imageName, maskName, steps, seed, denoise, grow }) {
    const wf = graphT2I(prompt, 1024, 1024, steps, seed);
    wf[7] = { class_type: 'LoadImage', inputs: { image: imageName } };
    wf[11] = { class_type: 'LoadImageMask', inputs: { image: maskName, channel: 'red' } };
    wf[12] = {
      class_type: 'VAEEncodeForInpaint',
      inputs: { pixels: ['7', 0], vae: ['4', 0], mask: ['11', 0], grow_mask_by: grow },
    };
    wf[8].inputs.latent_image = ['12', 0];
    wf[8].inputs.denoise = denoise;
    wf[10].inputs.filename_prefix = 'dsh_image_modes_inpaint';
    return wf;
  }

  async function comfySubmit(workflow, timeoutMs = 900000) {
    const clientId = 'dsh-image-modes';
    const queued = await withRetry('comfy queue', async () => {
      const res = await fetch(`${cfg.comfyHost}/prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: workflow, client_id: clientId }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status} ${text.slice(0, 300)}`);
      }
      return res.json();
    }, 2, 2000);
    if (queued.node_errors && Object.keys(queued.node_errors).length) {
      throw new Error(`ComfyUI rejected the graph: ${JSON.stringify(queued.node_errors).slice(0, 600)}`);
    }
    const pid = queued.prompt_id;
    if (!pid) throw new Error(`ComfyUI returned no prompt_id: ${JSON.stringify(queued).slice(0, 200)}`);

    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await sleep(2000);
      let hist;
      try {
        hist = await comfyGet(`/history/${pid}`);
      } catch (e) {
        continue;
      }
      const entry = hist && hist[pid];
      if (!entry) continue;
      const statusStr = entry?.status?.status_str || 'unknown';
      const files = [];
      for (const out of Object.values(entry.outputs || {})) {
        for (const img of out.images || []) {
          files.push(join(cfg.comfyRoot, 'output', img.subfolder || '', img.filename));
        }
      }
      return { status: statusStr, files, messages: entry?.status?.messages };
    }
    throw new Error(`ComfyUI timed out after ${Math.round(timeoutMs / 1000)}s`);
  }

  async function comfyUpload(localPath, name) {
    const inputDir = join(cfg.comfyRoot, 'input');
    await mkdir(inputDir, { recursive: true });
    const dest = join(inputDir, name);
    await copyFile(localPath, dest);
    return name;
  }

  async function localGenerate({ prompt, count, ratio, width, height, signal, steps, seed }) {
    await ensureLocal();
    let w;
    let h;
    if (width && height) {
      w = Number(width);
      h = Number(height);
    } else {
      const preset = LOCAL_RATIOS[ratio || '3:4'] || LOCAL_RATIOS['3:4'];
      [w, h] = preset;
    }
    if (w % 16 || h % 16) throw new Error(`local size ${w}x${h} must be a multiple of 16`);
    const px = w * h;
    const results = [];
    const n = Math.max(1, Math.min(16, Number(count) || 1));
    for (let i = 0; i < n; i += 1) {
      if (signal && signal.aborted) throw new Error('aborted');
      const s = seed && n === 1 ? Number(seed) : Math.floor(Math.random() * 2 ** 48);
      const wf = graphT2I(prompt, w, h, steps || cfg.steps, s);
      const out = await comfySubmit(wf);
      if (out.status !== 'success' || !out.files.length) {
        throw new Error(`local generation ${out.status}: ${JSON.stringify(out.messages || []).slice(0, 300)}`);
      }
      results.push({ seed: s, file: out.files[out.files.length - 1] });
    }
    return { width: w, height: h, px, images: results };
  }

  async function localInpaint({ prompt, basePath, maskPath, denoise, grow, steps, seed }) {
    await ensureLocal();
    const tag = Date.now().toString(36);
    const baseName = `dsh_modes_base_${tag}${extname(basePath) || '.png'}`;
    const maskName = `dsh_modes_mask_${tag}${extname(maskPath) || '.png'}`;
    await comfyUpload(basePath, baseName);
    await comfyUpload(maskPath, maskName);
    const s = seed ? Number(seed) : Math.floor(Math.random() * 2 ** 48);
    const wf = graphInpaint({
      prompt, imageName: baseName, maskName,
      steps: steps || cfg.steps, seed: s,
      denoise: Number.isFinite(denoise) ? denoise : 1.0,
      grow: Number.isFinite(grow) ? grow : 4,
    });
    const out = await comfySubmit(wf);
    if (out.status !== 'success' || !out.files.length) {
      throw new Error(`local inpaint ${out.status}: ${JSON.stringify(out.messages || []).slice(0, 300)}`);
    }
    return { seed: s, file: out.files[out.files.length - 1] };
  }

  // ── tools ─────────────────────────────────────────────────────────────────

  ctx.tools.register({
    name: 'image_modes_status',
    description: 'Report whether each image-generation mode is usable right now: cloud key/model/base, '
      + 'local ComfyUI reachability (version, GPU, free VRAM) and model filenames, plus the local pixel budget. '
      + 'Call this before a generation run to report capability honestly instead of promising a mode that cannot work.',
    parameters: { type: 'object', properties: {} },
    timeoutMs: 60000,
    async execute() {
      const cloud = await cloudEnv();
      const stats = await comfyStats();
      const dev = stats?.devices?.[0];
      const notes = [];
      if (!cloud.apiKey) notes.push('cloud: no API key resolved — set the IMAGE_API_KEY credential or config.apiKey');
      if (!stats) notes.push(`local: ComfyUI unreachable at ${cfg.comfyHost}` + (cfg.ensureServerScript ? ' (auto-start is configured)' : ' (no ensureServerScript configured)'));
      return {
        cloud: {
          configured: Boolean(cloud.apiKey),
          base: cloud.base,
          imageModel: cloud.imageModel,
          visionModel: cloud.visionModel,
          limits: 'no mask, no seed, repaints the whole frame; up to 4K tiers',
        },
        local: stats ? {
          reachable: true,
          version: stats?.system?.comfyui_version || 'unknown',
          device: dev?.name || 'unknown',
          vramFreeGB: dev?.vram_free ? Number((dev.vram_free / 1024 ** 3).toFixed(2)) : null,
          serverRoot: cfg.comfyRoot,
          unet: cfg.unet,
          clip: cfg.clip,
          vae: cfg.vae,
          autoStart: cfg.ensureServerScript || '(not configured)',
          pixelBudgetHint: 'keep every local size at or below ~1.03 MP (1344x768) on an 8 GB card',
          ratios: LOCAL_RATIOS,
        } : { reachable: false, host: cfg.comfyHost },
        notes,
      };
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, v) => {
        const lines = [
          `cloud : ${v.cloud.configured ? 'ready' : 'NOT configured'}  base=${v.cloud.base}  model=${v.cloud.imageModel}`,
          `        ${v.cloud.limits}`,
        ];
        if (v.local && v.local.reachable) {
          lines.push(`local : ready  ComfyUI ${v.local.version}  ${v.local.device}  free ${v.local.vramFreeGB}GB`);
          lines.push(`        root=${v.local.serverRoot}  autostart=${v.local.autoStart}`);
        } else {
          lines.push(`local : NOT reachable at ${v.local?.host || '?'}`);
        }
        for (const n of v.notes || []) lines.push(`note  : ${n}`);
        return [{ type: 'text', text: lines.join('\n') }];
      },
    },
  });

  ctx.tools.register({
    name: 'gen_image',
    description: 'Generate or edit images through one of three modes: "cloud" (remote OpenAI-compatible image API, '
      + 'up to 3.9 MP, understands the picture, no mask/seed), "local" (ComfyUI + Z-Image Turbo on this machine — '
      + 'free, reproducible seed, pixel-locked masked inpaint, <=1.03 MP) or "hybrid" (local drafts/base first, then a '
      + 'cloud hi-res refactor — the route that preserves a reference character best). '
      + `Tradeoffs, measured: ${TRADEOFFS} `
      + 'BEFORE CALLING: ask the user which mode they want and state the tradeoffs plus your recommendation — do not '
      + 'choose a mode silently. Useful defaults learned from testing: for a full-body figure use ratio 9:16 (a 3:4 '
      + 'frame crops the feet), and when repainting locally make the mask hug the object (a box over background makes '
      + 'the model repaint that background as flat white).',
    parameters: {
      type: 'object',
      properties: {
        mode: {
          type: 'string',
          enum: ['cloud', 'local', 'hybrid'],
          description: 'cloud: one remote call. local: ComfyUI on this machine (count drafts, or masked inpaint when image+mask are given). '
            + 'hybrid: local drafts (or a local base) first, then a cloud refactor of the chosen draft.',
        },
        prompt: { type: 'string', description: 'What to draw or how to change it. Describe materials and lighting; avoid "no/without X" phrasing.' },
        images: {
          type: 'array', items: { type: 'string' },
          description: 'Reference image paths or URLs. cloud: used as img2img base. hybrid: optional base for the local stage (outpaint).',
        },
        mask: { type: 'string', description: 'local only: white-on-black PNG mask (white = repaint) to run a pixel-locked inpaint on `image`.' },
        count: { type: 'number', description: 'local/hybrid: how many local drafts to make (default 4). More drafts = better odds, each 12–32s.' },
        ratio: { type: 'string', enum: [...CLOUD_RATIOS], description: 'Aspect ratio. 9:16 for full-body figures; local presets are fixed multiples of 16.' },
        size: { type: 'string', enum: [...CLOUD_SIZES], description: 'cloud resolution tier (default 2K ≈ 3.9 MP).' },
        width: { type: 'number', description: 'local only: exact width (multiple of 16), overrides ratio.' },
        height: { type: 'number', description: 'local only: exact height (multiple of 16), overrides ratio.' },
        denoise: { type: 'number', description: 'local inpaint strength: 0.5–0.7 keeps the existing silhouette (recolour/material), 0.85–1.0 invents (remove object, fill area). Default 1.0.' },
        grow: { type: 'number', description: 'local inpaint: grow_mask_by in pixels (default 4).' },
        seed: { type: 'number', description: 'local only: fixed seed reproduces the same image.' },
        pick: { type: 'boolean', description: 'hybrid: let the vision model rank the drafts and continue with the best one (default true).' },
        output: { type: 'string', description: 'Destination path for the final image (defaults to <outputDir>/<mode>_<timestamp>.png; never overwrites).' },
      },
      required: ['mode', 'prompt'],
    },
    timeoutMs: 1800000,
    async execute(args, exec) {
      const cwd = cwdOf(exec);
      const signal = exec && exec.signal;
      const mode = args.mode;
      const outDir = resolve(cwd, cfg.outputDir);
      await mkdir(outDir, { recursive: true });
      const stamp = Date.now();
      const wanted = args.output
        ? resolveIn(cwd, args.output)
        : join(outDir, `${mode}_${stamp}.png`);
      const result = { mode, files: [], notes: [] };

      if (mode === 'cloud') {
        const uris = [];
        for (const p of args.images || []) uris.push(await toDataUri(p, cwd));
        const g = await cloudGenerate({ prompt: args.prompt, images: uris, size: args.size, ratio: args.ratio, signal });
        const file = await saveUnique(wanted, g.buffer);
        result.files.push(file);
        result.notes.push(`cloud ${g.model} ${args.size || '2K'} ${args.ratio || '1:1'}`);
        result.remoteUrl = g.url;
      } else if (mode === 'local') {
        if (args.images && args.images.length && args.mask) {
          const base = resolveIn(cwd, args.images[0]);
          const mask = resolveIn(cwd, args.mask);
          const r = await localInpaint({
            prompt: args.prompt, basePath: base, maskPath: mask,
            denoise: args.denoise, grow: args.grow, steps: args.steps, seed: args.seed,
          });
          const buf = await readFile(r.file);
          result.files.push(await saveUnique(wanted, buf));
          result.seeds = [r.seed];
          result.notes.push(`local masked inpaint (denoise=${Number.isFinite(args.denoise) ? args.denoise : 1.0})`);
        } else {
          const r = await localGenerate({
            prompt: args.prompt, count: args.count || 4, ratio: args.ratio,
            width: args.width, height: args.height, steps: args.steps, seed: args.seed, signal,
          });
          for (let i = 0; i < r.images.length; i += 1) {
            const buf = await readFile(r.images[i].file);
            const target = i === 0 ? wanted : wanted.replace(/(\.\w+)$/, `-${i + 1}$1`);
            result.files.push(await saveUnique(target, buf));
          }
          result.seeds = r.images.map((x) => x.seed);
          result.notes.push(`local ${r.width}x${r.height} = ${r.px} px (${(r.px / 1e6).toFixed(2)} MP), ${r.images.length} drafts`);
          if (r.px > 1032192) result.notes.push('WARNING: above the ~1.03 MP budget of an 8 GB card');
        }
      } else if (mode === 'hybrid') {
        let draftPaths = [];
        let chain = '';
        if (args.images && args.images.length) {
          // Local base first: copy the reference through the local model as an
          // inpaint-free anchor is not possible without a mask, so treat the
          // reference as the cloud stage's input and say so.
          result.notes.push('hybrid with a reference image: the reference goes straight to the cloud refactor '
            + '(add `mask` to run a local pixel-locked pass instead)');
          draftPaths = [resolveIn(cwd, args.images[0])];
          chain = 'reference -> cloud';
        } else {
          const r = await localGenerate({
            prompt: args.prompt, count: args.count || 4, ratio: args.ratio,
            width: args.width, height: args.height, steps: args.steps, seed: args.seed, signal,
          });
          draftPaths = r.images.map((x) => x.file);
          result.seeds = r.images.map((x) => x.seed);
          result.drafts = draftPaths.slice();
          chain = `local ${draftPaths.length} drafts -> cloud`;
          result.notes.push(`local stage: ${r.width}x${r.height}, ${draftPaths.length} drafts`);
        }

        let chosen = draftPaths[0];
        if (args.pick !== false && draftPaths.length > 1) {
          try {
            const uris = [];
            for (const p of draftPaths) uris.push(await toDataUri(p, cwd));
            const rank = await visionRank(
              uris,
              `These are ${draftPaths.length} candidate images from the same prompt, numbered 1..${draftPaths.length}. `
              + 'Reply with ONLY the number of the single best candidate as a finished image (completeness of the subject, '
              + 'anatomy, hands, background cleanliness), then one short sentence of reason.',
              signal,
            );
            chosen = draftPaths[rank.idx] || chosen;
            result.picked = rank.idx + 1;
            result.pickReason = rank.text;
            chain += ` (vision picked #${rank.idx + 1})`;
          } catch (e) {
            result.notes.push(`vision pick skipped: ${e.message}`);
          }
        }
        result.chosenDraft = chosen;

        const uri = await toDataUri(chosen, cwd);
        const g = await cloudGenerate({
          prompt: `${args.prompt}\n\nKeep the character design, colours, costume and pose of the input image identical; `
            + 're-render it as a clean high-resolution finished illustration with crisp lineart, complete from head to toe, '
            + 'with a bit of empty background above and below.',
          images: [uri], size: args.size || '2K', ratio: args.ratio, signal,
        });
        const file = await saveUnique(wanted, g.buffer);
        result.files.push(file);
        result.remoteUrl = g.url;
        result.notes.push(`chain: ${chain}`);
      } else {
        throw new Error(`unknown mode "${mode}" (expected cloud | local | hybrid)`);
      }

      return result;
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, v) => {
        const lines = [`mode   : ${v.mode}`];
        if (v.drafts) lines.push(`drafts : ${v.drafts.length} local draft(s)`);
        if (v.picked) lines.push(`picked : #${v.picked}${v.pickReason ? ` — ${v.pickReason.split('\n')[0].slice(0, 160)}` : ''}`);
        if (v.seeds) lines.push(`seeds  : ${v.seeds.join(', ')}`);
        for (const f of v.files || []) lines.push(`file   : ${f}`);
        if (v.remoteUrl) lines.push(`remote : ${v.remoteUrl}`);
        for (const n of v.notes || []) lines.push(`note   : ${n}`);
        return [{ type: 'text', text: lines.join('\n') }];
      },
    },
  });

  ctx.tools.register({
    name: 'understand_image',
    description: 'Describe or analyze raster images through the configured multimodal model: explain content, read '
      + 'in-image text (OCR), judge style/composition, compare several images, or verify a generated result. Accepts '
      + 'local paths or public URLs; several images are analysed in ONE call, so ask for the comparison in `prompt`. '
      + 'This is the vision companion of gen_image and generates nothing itself.',
    parameters: {
      type: 'object',
      properties: {
        images: {
          type: 'array', items: { type: 'string' },
          description: 'Image paths (absolute or relative to the workspace) or public URLs.',
        },
        prompt: {
          type: 'string',
          description: 'What to extract, e.g. "Describe this image in detail", "Read all text verbatim", '
            + '"Which of these two is closer to X and why". Defaults to a detailed description.',
        },
      },
      required: ['images'],
    },
    timeoutMs: 180000,
    async execute(args, exec) {
      const cwd = cwdOf(exec);
      const list = args.images || [];
      if (!list.length) throw new Error('pass at least one image path or URL');
      const { apiKey, base, visionModel } = await cloudEnv();
      if (!apiKey) {
        throw new Error('understand_image needs an API key: set the IMAGE_API_KEY credential '
          + '(or config.apiKey).');
      }
      const uris = [];
      for (const p of list) uris.push(await toDataUri(p, cwd));
      const content = uris.map((u) => ({ type: 'image_url', image_url: { url: u } }));
      content.push({ type: 'text', text: args.prompt || 'Describe this image in detail.' });
      const data = await withRetry('vision chat', async () => {
        const res = await fetch(`${base}/v1/chat/completions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
          body: JSON.stringify({
            model: visionModel,
            messages: [{ role: 'user', content }],
            temperature: 0.3,
            max_tokens: 4096,
          }),
        });
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(`HTTP ${res.status}: ${text.slice(0, 300)}`);
        }
        return res.json();
      }, 3, 2000);
      const text = String(data?.choices?.[0]?.message?.content || '').trim();
      if (!text) throw new Error('vision model returned no content');
      return { model: visionModel, images: list, text };
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, v) => [{ type: 'text', text: `${v.model}:\n${v.text}` }],
    },
  });
}
