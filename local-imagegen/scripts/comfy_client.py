"""Shared ComfyUI client for the local-imagegen skill.

Everything the skill needs to talk to a running ComfyUI instance:
readiness probe, prompt submission, history polling, and workflow builders.

Environment overrides:
  LOCAL_IMAGEGEN_ROOT   ComfyUI install directory (default D:\\dsh\\ComfyUI)
  LOCAL_IMAGEGEN_HOST   ComfyUI base URL (default http://127.0.0.1:8188)
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

COMFY_ROOT = os.environ.get('LOCAL_IMAGEGEN_ROOT', r'D:\dsh\ComfyUI')
HOST = os.environ.get('LOCAL_IMAGEGEN_HOST', 'http://127.0.0.1:8188')
OUTPUT_DIR = os.path.join(COMFY_ROOT, 'output')
INPUT_DIR = os.path.join(COMFY_ROOT, 'input')

UNET = os.environ.get('LOCAL_IMAGEGEN_UNET', 'z_image_turbo_int8_convrot.safetensors')
CLIP = os.environ.get('LOCAL_IMAGEGEN_CLIP', 'qwen_3_4b_fp8_mixed.safetensors')
VAE = os.environ.get('LOCAL_IMAGEGEN_VAE', 'ae.safetensors')

# 分辨率预设，宽高都必须是 16 的倍数（VAE 8x 下采样 + patch 2x）
RATIOS = {
    '1:1': (1024, 1024),
    '4:3': (1152, 896),
    '3:4': (896, 1152),
    '3:2': (1216, 832),
    '2:3': (832, 1216),
    '16:9': (1344, 768),
    '9:16': (768, 1344),
    '21:9': (1536, 640),
}

# Z-Image Turbo 的官方推荐采样参数（来自 Comfy-Org 工作流模板）
SAMPLER = 'res_multistep'
SCHEDULER = 'simple'
CFG = 1.0
AURAFLOW_SHIFT = 3
DEFAULT_STEPS = 8


class ComfyUnavailable(RuntimeError):
    pass


def _request(path, payload=None, timeout=60):
    url = HOST + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={'User-Agent': 'local-imagegen-skill'})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    return json.loads(body) if body.strip() else {}


def get(path, timeout=30):
    return _request(path, None, timeout)


def post(path, payload, timeout=60):
    return _request(path, payload, timeout)


def is_up(timeout=5):
    try:
        get('/system_stats', timeout)
        return True
    except Exception:
        return False


def wait_ready(timeout=600, interval=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_up():
            return get('/system_stats')
        time.sleep(interval)
    raise ComfyUnavailable(
        'ComfyUI did not become ready at %s within %ds. Start it first by running '
        'ensure_server.ps1 from the local-imagegen skill scripts directory.'
        % (HOST, timeout))


def require_up():
    if not is_up():
        raise ComfyUnavailable(
            'ComfyUI is not reachable at %s.\n'
            'Run the skill helper first (no pwsh on this host, invoke directly):\n'
            '  & \'<skill>\\scripts\\ensure_server.ps1\'' % HOST)


def _base_nodes(prompt, width, height, steps, seed):
    return {
        '1': {'class_type': 'UNETLoader',
              'inputs': {'unet_name': UNET, 'weight_dtype': 'default'}},
        '2': {'class_type': 'ModelSamplingAuraFlow',
              'inputs': {'model': ['1', 0], 'shift': AURAFLOW_SHIFT}},
        '3': {'class_type': 'CLIPLoader',
              'inputs': {'clip_name': CLIP, 'type': 'lumina2', 'device': 'default'}},
        '4': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        '5': {'class_type': 'CLIPTextEncode',
              'inputs': {'clip': ['3', 0], 'text': prompt}},
        '6': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['5', 0]}},
        '7': {'class_type': 'EmptySD3LatentImage',
              'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '8': {'class_type': 'KSampler',
              'inputs': {'model': ['2', 0], 'positive': ['5', 0], 'negative': ['6', 0],
                         'latent_image': ['7', 0], 'seed': seed, 'steps': steps,
                         'cfg': CFG, 'sampler_name': SAMPLER, 'scheduler': SCHEDULER,
                         'denoise': 1.0}},
        '9': {'class_type': 'VAEDecode', 'inputs': {'samples': ['8', 0], 'vae': ['4', 0]}},
        '10': {'class_type': 'SaveImage',
               'inputs': {'images': ['9', 0], 'filename_prefix': 'local_imagegen'}},
    }


def build_t2i(prompt, width, height, steps=DEFAULT_STEPS, seed=None):
    """Text-to-image graph. cfg=1 means negative prompts have no effect."""
    if seed is None:
        seed = random.randint(0, 2 ** 48)
    return _base_nodes(prompt, width, height, steps, seed), seed


def build_inpaint(image_name, mask_name, prompt, steps=DEFAULT_STEPS,
                  seed=None, grow_mask_by=4, denoise=1.0):
    """Masked repaint graph. image_name/mask_name must already be in ComfyUI/input.

    denoise controls how much of the masked area is reinvented, and it is the single
    most important knob for local editing:

      0.5-0.7  keeps the existing silhouette and re-renders its appearance
               (recolour, change material, fix a detail)
      0.85-1.0 invents content (remove an object, fill empty area)

    Pinning it at 1.0 -- as this skill did originally -- makes every masked edit a
    full repaint, so a large mask produces an unrelated picture inside the mask.
    """
    if seed is None:
        seed = random.randint(0, 2 ** 48)
    wf = _base_nodes(prompt, 1024, 1024, steps, seed)
    wf['7'] = {'class_type': 'LoadImage', 'inputs': {'image': image_name}}
    wf['11'] = {'class_type': 'LoadImageMask',
                'inputs': {'image': mask_name, 'channel': 'red'}}
    wf['12'] = {'class_type': 'VAEEncodeForInpaint',
                'inputs': {'pixels': ['7', 0], 'vae': ['4', 0], 'mask': ['11', 0],
                           'grow_mask_by': grow_mask_by}}
    wf['8']['inputs']['latent_image'] = ['12', 0]
    wf['8']['inputs']['denoise'] = float(denoise)
    wf['10']['inputs']['filename_prefix'] = 'local_imagegen_inpaint'
    return wf, seed


def submit(workflow, timeout=900, poll=2.0, verbose=True):
    """Queue one workflow and block until it reaches a terminal state.

    Returns (status_str, [absolute output paths]).
    """
    r = post('/prompt', {'prompt': workflow, 'client_id': 'local-imagegen'})
    node_errors = r.get('node_errors')
    if node_errors:
        raise RuntimeError('ComfyUI rejected the graph: %s'
                           % json.dumps(node_errors, ensure_ascii=False)[:800])
    pid = r.get('prompt_id')
    if not pid:
        raise RuntimeError('ComfyUI returned no prompt_id: %s' % r)

    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            hist = get('/history/%s' % pid)
        except Exception:
            hist = {}
        if pid in hist:
            entry = hist[pid]
            status = entry.get('status', {})
            status_str = status.get('status_str', 'unknown')
            files = []
            for out in entry.get('outputs', {}).values():
                for img in out.get('images', []):
                    files.append(os.path.join(
                        OUTPUT_DIR, img.get('subfolder', ''), img['filename']))
            if status_str != 'success' and verbose:
                print('[status] %s' % json.dumps(
                    status.get('messages'), ensure_ascii=False)[:1200])
            return status_str, files
        if verbose and int(time.time() - t0) % 30 < poll:
            pass
        time.sleep(poll)
    return 'timeout', []


def resolve_size(ratio=None, width=None, height=None):
    if width and height:
        return int(width), int(height)
    if ratio:
        if ratio not in RATIOS:
            raise ValueError('unknown ratio %r; known: %s'
                             % (ratio, ', '.join(sorted(RATIOS))))
        return RATIOS[ratio]
    return RATIOS['1:1']


def read_prompt(args):
    """Read the prompt from --prompt-file (UTF-8) or --prompt.

    Prefer the file form: passing CJK text through a Windows shell command line
    is encoding-fragile.
    """
    if getattr(args, 'prompt_file', None):
        with open(args.prompt_file, encoding='utf-8') as f:
            return f.read().strip()
    if getattr(args, 'prompt', None):
        return args.prompt
    raise SystemExit('need --prompt-file (preferred) or --prompt')
