"""Masked repaint (inpainting) against a local ComfyUI.

Use this to fix a local region of an existing render instead of regenerating the
whole image. Two mask sources:

  --rect x0,y0,x1,y1     axis-aligned rectangle (image pixel coordinates)
  --poly  "x,y x,y ..."  polygon; use a step shape that hugs the object so the
                         model does not have to invent background
  --mask  path.png       ready-made mask (white = repaint)

The mask shape matters a lot: a mask that exposes background forces the model to
hallucinate that background, which usually produces flat smears and seam rings.
Keep masks tight to the object being replaced.

--denoise controls how much of the masked area is reinvented: 0.5-0.7 keeps the
existing silhouette (recolour / material changes), 0.85-1.0 invents content
(object removal, filling empty areas). --mask-blur softens the mask the model
sees, which is what removes the hard seam ring a binary mask produces.

Usage:
  python inpaint.py --image out.png --rect 1114,172,1250,268 --prompt-file fix.txt
"""

import argparse
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy_client as cc

# Windows 管道下 Python 默认用 ANSI 代码页，中文提示会乱码。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = None


def build_mask(image_path, out_path, rect=None, poly=None, supplied=None, blur=0):
    if supplied:
        shutil.copyfile(supplied, out_path)
        if blur and blur > 0:
            if Image is None:
                raise SystemExit('Pillow is required for --mask-blur')
            Image.open(out_path).convert('L').filter(
                ImageFilter.GaussianBlur(blur)).save(out_path)
        return
    if Image is None:
        raise SystemExit('Pillow is required to build a mask; pass --mask instead')
    im = Image.open(image_path)
    w, h = im.size
    mask = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(mask)
    if rect:
        d.rectangle(rect, fill=255)
    elif poly:
        pts = []
        for pair in poly.replace(';', ' ').split():
            x, y = pair.split(',')
            pts.append((float(x), float(y)))
        if len(pts) < 3:
            raise SystemExit('--poly needs at least 3 "x,y" points')
        d.polygon(pts, fill=255)
    else:
        raise SystemExit('one of --rect / --poly / --mask is required')
    if blur and blur > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    mask.save(out_path)


def main():
    ap = argparse.ArgumentParser(description='Local inpainting (ComfyUI / Z-Image Turbo)')
    ap.add_argument('--image', required=True, help='source PNG to edit')
    ap.add_argument('--rect', help='x0,y0,x1,y1')
    ap.add_argument('--poly', help='"x,y x,y ..." polygon')
    ap.add_argument('--mask', help='ready-made mask PNG (white = repaint)')
    ap.add_argument('--prompt')
    ap.add_argument('--prompt-file', dest='prompt_file')
    ap.add_argument('--steps', type=int, default=cc.DEFAULT_STEPS)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--grow', type=int, default=4, help='grow_mask_by (px)')
    ap.add_argument('--denoise', type=float, default=1.0,
                    help='repaint strength: 0.5-0.7 keeps the silhouette '
                         '(recolour / material), 0.85-1.0 invents (removal, fill)')
    ap.add_argument('--mask-blur', dest='mask_blur', type=int, default=0,
                    help='blur the mask the model sees (px) -> soft edge, no seam ring')
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--json', action='store_true', dest='as_json')
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        raise SystemExit('no such image: %s' % args.image)
    prompt = cc.read_prompt(args)
    cc.require_up()

    tag = uuid.uuid4().hex[:10]
    base_name = 'ig_base_%s.png' % tag
    mask_name = 'ig_mask_%s.png' % tag
    os.makedirs(cc.INPUT_DIR, exist_ok=True)

    rect = None
    if args.rect:
        rect = tuple(float(v) for v in args.rect.split(','))
        if len(rect) != 4:
            raise SystemExit('--rect needs 4 comma-separated numbers')

    shutil.copyfile(args.image, os.path.join(cc.INPUT_DIR, base_name))
    build_mask(args.image, os.path.join(cc.INPUT_DIR, mask_name),
               rect=rect, poly=args.poly, supplied=args.mask,
               blur=args.mask_blur)

    wf, seed = cc.build_inpaint(base_name, mask_name, prompt, args.steps,
                                args.seed, args.grow, denoise=args.denoise)
    t0 = time.time()
    status, files = cc.submit(wf, timeout=args.timeout, verbose=not args.as_json)
    elapsed = time.time() - t0

    if args.as_json:
        print(json.dumps({'ok': status == 'success', 'status': status, 'seed': seed,
                          'seconds': round(elapsed, 1), 'files': files},
                         ensure_ascii=False, indent=1))
    else:
        print('[%s] %.1fs  seed=%d' % (status, elapsed, seed))
        for f in files:
            print(f)
    return 0 if status == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
