"""Preflight capability check for local image generation.

Answers one question: can this machine run local image generation right now, and
should the agent ask the user before enabling it?

Local generation is a heavy GPU workload, so this runs BEFORE anything starts a
ComfyUI instance or loads a model. Default output is a short human-readable
report; --json emits the structured verdict the agent branches on.

  python check_capability.py
  python check_capability.py --json
  python check_capability.py --fast      # skip the torch/CUDA probe
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 管道下 Python 默认用 ANSI 代码页，中文会乱码；统一按 UTF-8 输出。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

COMFY_ROOT = os.environ.get('LOCAL_IMAGEGEN_ROOT', r'D:\dsh\ComfyUI')

# 已实测可用的组合：RTX 5060 Laptop 8GB + int8 量化模型
# 阈值按 MiB 计：标称 8GB 的卡实际可用约 8151 MiB，不能拿 8192 当门槛。
MIN_VRAM_MB = 6 * 1024          # 低于此值不建议启用
GOOD_VRAM_MB = 7680             # 推荐下限（7.5 GiB）
COMFORT_VRAM_MB = 12 * 1024     # 舒适区间

MODELS = {
    r'models\diffusion_models\z_image_turbo_int8_convrot.safetensors': 5.78,
    r'models\text_encoders\qwen_3_4b_fp8_mixed.safetensors': 5.25,
    r'models\vae\ae.safetensors': 0.31,
}


def probe_gpu():
    """Return (gpus, error). Each gpu: name, vram_mb, driver, compute_cap."""
    exe = shutil.which('nvidia-smi')
    if not exe:
        return [], 'nvidia-smi not found (no NVIDIA driver installed)'
    try:
        p = subprocess.run(
            [exe, '--query-gpu=name,memory.total,driver_version,compute_cap',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=25)
    except Exception as e:
        return [], '%s: %s' % (type(e).__name__, e)
    if p.returncode != 0:
        return [], 'nvidia-smi exited %d: %s' % (p.returncode, p.stderr.strip()[:200])
    gpus = []
    for line in p.stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(',')]
        if len(parts) < 4:
            continue
        gpus.append({'name': parts[0],
                     'vram_mb': int(float(parts[1])),
                     'driver': parts[2],
                     'compute_cap': parts[3]})
    return gpus, None if gpus else 'nvidia-smi reported no GPUs'


def venv_python():
    return os.path.join(COMFY_ROOT, 'venv', 'Scripts', 'python.exe')


def probe_cuda():
    """Import torch in the ComfyUI venv and report the CUDA runtime state."""
    py = venv_python()
    if not os.path.isfile(py):
        return {'available': False, 'reason': 'ComfyUI venv python missing at %s' % py}
    code = (
        'import json,torch;'
        'print(json.dumps({'
        '"torch": torch.__version__,'
        '"cuda_build": torch.version.cuda,'
        '"available": torch.cuda.is_available(),'
        '"capability": list(torch.cuda.get_device_capability(0)) if torch.cuda.is_available() else None,'
        '"device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,'
        '"bf16": bool(torch.cuda.is_bf16_supported()) if torch.cuda.is_available() else False'
        '}))')
    try:
        p = subprocess.run([py, '-c', code], capture_output=True, text=True, timeout=180)
    except Exception as e:
        return {'available': False, 'reason': '%s: %s' % (type(e).__name__, e)}
    if p.returncode != 0:
        return {'available': False,
                'reason': 'torch probe failed: %s' % (p.stderr.strip()[-300:] or 'unknown')}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {'available': False, 'reason': 'unparsable torch probe output'}


def probe_install():
    out = {'root': COMFY_ROOT, 'root_exists': os.path.isdir(COMFY_ROOT),
           'main_py': os.path.isfile(os.path.join(COMFY_ROOT, 'main.py')),
           'venv': os.path.isfile(venv_python()), 'models': {}}
    for rel, expect_gb in MODELS.items():
        path = os.path.join(COMFY_ROOT, rel)
        size_gb = os.path.getsize(path) / 2**30 if os.path.isfile(path) else 0.0
        out['models'][rel] = {'exists': size_gb > 0, 'size_gb': round(size_gb, 2),
                              'expected_gb': expect_gb,
                              'complete': size_gb >= expect_gb * 0.95}
    return out


def probe_server():
    try:
        import comfy_client as cc
        if not cc.is_up():
            return {'up': False}
        st = cc.get('/system_stats')
        dev = (st.get('devices') or [{}])[0]
        return {'up': True, 'version': st.get('system', {}).get('comfyui_version'),
                'vram_free_gb': round(dev.get('vram_free', 0) / 2**30, 2)}
    except Exception as e:
        return {'up': False, 'error': '%s: %s' % (type(e).__name__, e)}


def decide(gpus, gpu_err, cuda, install, server, fast):
    """Fold the probes into (status, tier, eligible, max_pixels, reasons[])."""
    reasons = []

    if not gpus:
        return ('unsupported', 'none', False, 0,
                ['未检测到 NVIDIA 显卡或驱动：%s' % (gpu_err or 'unknown')])

    best = max(gpus, key=lambda g: g['vram_mb'])
    vram = best['vram_mb']

    if not fast:
        if not cuda.get('available'):
            return ('unsupported', 'none', False, 0,
                    ['CUDA 运行时不可用：%s' % cuda.get('reason', 'unknown')])
        cap = cuda.get('capability')
        if cap and cap[0] < 7:
            return ('unsupported', 'none', False, 0,
                    ['显卡计算能力 %s 过低（需 7.0 以上）' % '.'.join(map(str, cap))])

    if not install['main_py'] or not install['venv']:
        return ('needs_setup', 'unknown', True, 0,
                ['ComfyUI 未安装或 venv 缺失（%s）' % install['root']])

    missing = [r for r, m in install['models'].items() if not m['complete']]
    if missing:
        return ('needs_setup', 'unknown', True, 0,
                ['模型文件缺失或未下载完整：%s' % '、'.join(
                    os.path.basename(m) for m in missing)])

    if vram < MIN_VRAM_MB:
        return ('ready_degraded', 'insufficient', False, 0,
                ['显存仅 %d MB，低于最低要求 %d MB，本地生图不可用'
                 % (vram, MIN_VRAM_MB)])

    if vram < GOOD_VRAM_MB:
        tier, max_px = 'tight', 640 * 640
        reasons.append('显存 %d MB 低于推荐值 %d MB，出图较慢且分辨率受限'
                       % (vram, GOOD_VRAM_MB))
    elif vram < COMFORT_VRAM_MB:
        tier, max_px = 'ok', 1344 * 768
        reasons.append('显存 %d MB，属于已实测可用的区间' % vram)
    else:
        tier, max_px = 'comfortable', 2048 * 2048
        reasons.append('显存 %d MB，余量充足，可上 2K' % vram)

    if not fast and cuda.get('cuda_build'):
        try:
            major = int(str(cuda['cuda_build']).split('.')[0])
            if major < 13:
                reasons.append('PyTorch 构建为 CUDA %s，ComfyUI 加速内核（comfy_kitchen）'
                               '需要 cu130 且驱动 580+，当前回落到 eager 路径，速度非最优'
                               % cuda['cuda_build'])
        except Exception:
            pass

    status = 'ready' if tier in ('ok', 'comfortable') and not reasons[1:] else 'ready_degraded'
    if tier in ('ok', 'comfortable') and len(reasons) == 1:
        status = 'ready'
    return (status, tier, True, max_px, reasons)


def build_prompt(status, tier, gpu, vram_mb, reasons):
    """The exact question the agent must put to the user before enabling."""
    gpu_desc = ('%s（%.0f GB 显存）' % (gpu['name'], vram_mb / 1024)) if gpu \
        else '未检测到可用显卡'
    tail = ('；'.join(reasons) + '。') if reasons else ''

    if status == 'needs_setup':
        return ('本地生图需要高性能显卡。检测到 %s，但环境尚未就绪。%s'
                '是否现在安装 ComfyUI 与模型？' % (gpu_desc, tail))
    if tier == 'insufficient':
        return ('本地生图需要高性能显卡。%s。%s本地生图无法启用；'
                '是否改用云端生图方案？' % (gpu_desc, tail))
    if status == 'unsupported':
        return ('本地生图需要高性能显卡。%s。%s本地生图无法启用；'
                '是否改用云端生图方案？' % (gpu_desc, tail))
    if tier == 'tight':
        return ('本地生图需要高性能显卡。检测结果：%s，可以运行但性能偏紧。%s'
                '是否仍要启用本地生图？' % (gpu_desc, tail))
    return ('本地生图需要高性能显卡。检测结果：%s，满足要求。%s是否启用本地生图？'
            % (gpu_desc, tail))


def main():
    ap = argparse.ArgumentParser(description='Local image generation capability check')
    ap.add_argument('--json', action='store_true', dest='as_json')
    ap.add_argument('--fast', action='store_true',
                    help='skip the torch/CUDA probe (no venv import)')
    args = ap.parse_args()

    gpus, gpu_err = probe_gpu()
    best = max(gpus, key=lambda g: g['vram_mb']) if gpus else None
    cuda = {} if args.fast else probe_cuda()
    install = probe_install()
    server = probe_server()

    status, tier, eligible, max_px, reasons = decide(
        gpus, gpu_err, cuda, install, server, args.fast)
    prompt = build_prompt(status, tier, best, best['vram_mb'] if best else 0, reasons)

    result = {
        'status': status,              # ready | ready_degraded | needs_setup | unsupported
        'tier': tier,                  # comfortable | ok | tight | insufficient | none | unknown
        'eligible': eligible,          # is local generation technically possible at all
        'requiresUserConsent': True,   # always ask before turning on a heavy GPU workload
        'enablePrompt': prompt,        # put this to the user verbatim
        'maxPixels': max_px,
        'reasons': reasons,
        'gpu': best,
        'gpus': gpus,
        'cuda': cuda,
        'install': install,
        'server': server,
    }

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print('本地生图能力检测')
        print('  结论   : %s / %s' % (status, tier))
        print('  显卡   : %s' % (best['name'] if best else '无 (%s)' % (gpu_err or '?')))
        if best:
            print('  显存   : %d MB   驱动 %s   算力 %s'
                  % (best['vram_mb'], best['driver'], best['compute_cap']))
        if not args.fast:
            print('  CUDA   : available=%s build=%s device=%s'
                  % (cuda.get('available'), cuda.get('cuda_build'), cuda.get('device')))
        print('  ComfyUI: root=%s main.py=%s venv=%s'
              % (install['root_exists'], install['main_py'], install['venv']))
        for rel, m in install['models'].items():
            print('  模型   : %-52s %s (%.2f GB)'
                  % (os.path.basename(rel), 'OK' if m['complete'] else '缺失',
                     m['size_gb']))
        print('  服务   : %s' % ('运行中 %s' % server.get('version') if server['up'] else '未启动'))
        print('  建议上限: %s 像素' % (max_px or 'n/a'))
        for r in reasons:
            print('  说明   : %s' % r)
        print()
        print('  需向用户确认：')
        print('    %s' % prompt)

    return 0 if eligible else 2


if __name__ == '__main__':
    raise SystemExit(main())
