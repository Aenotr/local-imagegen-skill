"""Text-to-image against a local ComfyUI (Z-Image Turbo).

Usage:
  python generate.py --prompt-file prompt.txt --ratio 16:9
  python generate.py --prompt-file prompt.txt --width 1344 --height 768 --count 4
  python generate.py --prompt-file prompt.txt --ratio 3:4 --seed 12345 --json

Prints one line per produced image: the absolute PNG path.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy_client as cc

# Windows 管道下 Python 默认用 ANSI 代码页，中文提示会乱码。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')


def main():
    ap = argparse.ArgumentParser(description='Local text-to-image (ComfyUI / Z-Image Turbo)')
    ap.add_argument('--prompt')
    ap.add_argument('--prompt-file', dest='prompt_file',
                    help='UTF-8 file holding the prompt (preferred on Windows)')
    ap.add_argument('--ratio', help='one of: ' + ', '.join(sorted(cc.RATIOS)))
    ap.add_argument('--width', type=int)
    ap.add_argument('--height', type=int)
    ap.add_argument('--steps', type=int, default=cc.DEFAULT_STEPS)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--count', type=int, default=1,
                    help='number of independent images (distinct seeds)')
    ap.add_argument('--timeout', type=int, default=900, help='per-image seconds')
    ap.add_argument('--json', action='store_true', dest='as_json')
    args = ap.parse_args()

    prompt = cc.read_prompt(args)
    width, height = cc.resolve_size(args.ratio, args.width, args.height)
    cc.require_up()

    results = []
    for i in range(max(1, args.count)):
        seed = args.seed if (args.seed is not None and args.count == 1) else None
        wf, used_seed = cc.build_t2i(prompt, width, height, args.steps, seed)
        t0 = time.time()
        status, files = cc.submit(wf, timeout=args.timeout, verbose=not args.as_json)
        elapsed = time.time() - t0
        if not args.as_json:
            print('[%d/%d] %s  %.1fs  seed=%d  %dx%d'
                  % (i + 1, args.count, status, elapsed, used_seed, width, height),
                  flush=True)
            for f in files:
                print(f)
        results.append({'status': status, 'seed': used_seed, 'seconds': round(elapsed, 1),
                        'width': width, 'height': height, 'files': files})

    if args.as_json:
        print(json.dumps({'ok': all(r['status'] == 'success' for r in results),
                          'images': results}, ensure_ascii=False, indent=1))
    return 0 if all(r['status'] == 'success' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
