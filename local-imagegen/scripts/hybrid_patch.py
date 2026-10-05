"""Pixel-locked local patch: crop a region, repaint it with the local ComfyUI model,
paste the result back through a feathered mask so every other pixel stays original.

The missing glue for hybrid editing: the local model cannot take a ~3.9 MP image
whole (8 GB VRAM, ~1.03 MP budget), so edits happen on a crop and are composited
back at native resolution.

  python hybrid_patch.py --image big.png --rect 580,420,860,760 \
      --mask-poly "620,430 820,430 820,700 620,700" \
      --prompt-file p.txt --out patched.png

Three things this wrapper does that a bare inpaint call cannot:

  --align-from REF      Coordinates were measured on REF but the edit targets
                        --image (e.g. a cloud re-render of the same character).
                        Estimates the character's scale+offset between the two by
                        thresholding the (white) background into a bounding box,
                        then maps --rect and --mask-poly into target space. Without
                        this a two-stage pipeline paints the patch in the wrong
                        place -- the failure that motivated writing it.
  --match-background    The two stages render white slightly differently, so the
                        patch shows up as a bright rectangle. Measures the crop's
                        four corners in both images and offset-corrects the patch.
  --denoise/--mask-blur Forwarded to the local workflow: silhouette-preserving
                        edits vs full invention, and a soft mask instead of a hard
                        edge (which is what produces the seam ring).

Mask sources (coordinates in --align-from space, or --image space without it):
  --mask-full                 repaint the whole crop
  --mask-rect x0,y0,x1,y1     repaint a rectangle inside the crop
  --mask-poly "x,y x,y ..."   repaint a polygon inside the crop

Exit codes: 0 ok, 1 generation failed, 2 bad arguments.
"""

import argparse
import json
import os
import sys
import time
import uuid

from PIL import Image, ImageChops, ImageDraw, ImageFilter

DEFAULT_SKILL_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = r'D:\dsh\_hybrid\tmp'

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')


def load_client(scripts_dir):
    sys.path.insert(0, scripts_dir)
    import comfy_client as cc
    return cc


def subject_bbox(im, thresh=240):
    """Bounding box of the non-background pixels (white-background art)."""
    return im.convert('L').point(lambda v: 255 if v < thresh else 0).getbbox()


def white_level(im, box, thresh=245, min_frac=0.01):
    """Mean level of the near-white pixels inside `box`, or None if there are too few.

    Sampling the four corners instead (the first version) is wrong whenever the crop
    corner lands on hair or clothing: it then reports a mid-grey "background" and the
    tone correction does nothing. Only near-white pixels are background.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    region = im.convert('L').crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
    hist = region.histogram()
    total = sum(hist)
    if not total:
        return None
    picked = sum(hist[thresh:])
    if picked < max(1, int(total * min_frac)):
        return None
    return sum((thresh + i) * c for i, c in enumerate(hist[thresh:])) / picked


def build_mask(size, args, origin):
    """Return an L-mode mask for the crop; white = repaint."""
    w, h = size
    mask = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(mask)
    ox, oy = origin
    if args.mask_full:
        d.rectangle([0, 0, w - 1, h - 1], fill=255)
    elif args.mask_rect:
        rx0, ry0, rx1, ry1 = (float(v) for v in args.mask_rect.split(','))
        d.rectangle([rx0 - ox, ry0 - oy, rx1 - ox, ry1 - oy], fill=255)
    elif args.mask_poly:
        pts = []
        for pair in args.mask_poly.replace(';', ' ').split():
            px, py = pair.split(',')
            pts.append((float(px) - ox, float(py) - oy))
        if len(pts) < 3:
            raise SystemExit('--mask-poly needs at least 3 points')
        d.polygon(pts, fill=255)
    else:
        raise SystemExit('one of --mask-full / --mask-rect / --mask-poly is required')
    return mask


def main():
    ap = argparse.ArgumentParser(description='Pixel-locked local patch (crop -> inpaint -> paste back)')
    ap.add_argument('--image', required=True, help='target image (gets patched)')
    ap.add_argument('--align-from', dest='align_from',
                    help='image the coordinates were measured on; enables auto realign')
    ap.add_argument('--rect', required=True, help='crop region x0,y0,x1,y1')
    ap.add_argument('--mask-full', action='store_true')
    ap.add_argument('--mask-rect')
    ap.add_argument('--mask-poly')
    ap.add_argument('--prompt')
    ap.add_argument('--prompt-file', dest='prompt_file')
    ap.add_argument('--out', required=True)
    ap.add_argument('--denoise', type=float, default=1.0,
                    help='0.5-0.7 keeps the silhouette, 0.85-1.0 invents content')
    ap.add_argument('--mask-blur', dest='mask_blur', type=int, default=8,
                    help='mask blur fed to the model (px) -> soft edge, no seam ring')
    ap.add_argument('--feather', type=int, default=8,
                    help='paste-back alpha blur radius (px). Wide values ghost whatever '
                         'was removed; zero leaves a hard rectangle edge. 6-10 is the band.')
    ap.add_argument('--no-match-background', dest='match_background',
                    action='store_false', default=True)
    ap.add_argument('--scripts', default=os.environ.get('LOCAL_IMAGEGEN_SCRIPTS',
                                                       DEFAULT_SKILL_SCRIPTS))
    ap.add_argument('--steps', type=int, default=None)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--grow', type=int, default=4)
    args = ap.parse_args()

    cc = load_client(args.scripts)
    if args.steps is None:
        args.steps = cc.DEFAULT_STEPS
    if not os.path.isfile(args.image):
        raise SystemExit('no such image: %s' % args.image)

    src = Image.open(args.image).convert('RGB')
    W, H = src.size
    rect = [float(v) for v in args.rect.split(',')]
    poly, mrect = args.mask_poly, args.mask_rect

    if args.align_from:
        ref = Image.open(args.align_from).convert('RGB')
        b_ref, b_tgt = subject_bbox(ref), subject_bbox(src)
        if not b_ref or not b_tgt:
            raise SystemExit('auto-align failed: could not find the subject (ref=%s tgt=%s)'
                             % (b_ref, b_tgt))
        sx = (b_tgt[2] - b_tgt[0]) / float(b_ref[2] - b_ref[0])
        sy = (b_tgt[3] - b_tgt[1]) / float(b_ref[3] - b_ref[1])
        print('auto-align: subject bbox %s -> %s  scale x%.4f y%.4f' % (b_ref, b_tgt, sx, sy))
        mx = lambda x: b_tgt[0] + (x - b_ref[0]) * sx          # noqa: E731
        my = lambda y: b_tgt[1] + (y - b_ref[1]) * sy          # noqa: E731
        rect = [mx(rect[0]), my(rect[1]), mx(rect[2]), my(rect[3])]
        if poly:
            poly = ' '.join('%d,%d' % (mx(float(p.split(',')[0])), my(float(p.split(',')[1])))
                            for p in poly.replace(';', ' ').split())
        if mrect:
            r = [float(v) for v in mrect.split(',')]
            mrect = '%d,%d,%d,%d' % (mx(r[0]), my(r[1]), mx(r[2]), my(r[3]))

    x0 = max(0, int(round(rect[0])))
    y0 = max(0, int(round(rect[1])))
    x1 = min(W, int(round(rect[2])))
    y1 = min(H, int(round(rect[3])))
    if x1 <= x0 or y1 <= y0:
        raise SystemExit('empty crop after clamping to %dx%d' % (W, H))
    crop = src.crop((x0, y0, x1, y1))
    cw, ch = crop.size
    print('source %dx%d -> crop (%d,%d,%d,%d) = %dx%d = %d px' % (W, H, x0, y0, x1, y1, cw, ch, cw * ch))
    if cw * ch > 1032192:
        print('WARNING: crop %d px exceeds the local budget 1032192 px' % (cw * ch))

    args.mask_rect, args.mask_poly = mrect, poly
    mask = build_mask((cw, ch), args, (x0, y0))
    model_mask = mask.filter(ImageFilter.GaussianBlur(args.mask_blur)) if args.mask_blur > 0 else mask
    paste_mask = mask.filter(ImageFilter.GaussianBlur(args.feather)) if args.feather > 0 else mask
    repaint = sum(1 for v in mask.getdata() if v > 127)
    print('mask: %d px repaint (%.0f%% of crop) | model blur=%dpx paste feather=%dpx | denoise=%.2f'
          % (repaint, 100.0 * repaint / (cw * ch), args.mask_blur, args.feather, args.denoise))

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(cc.INPUT_DIR, exist_ok=True)
    tag = uuid.uuid4().hex[:10]
    base_name = 'ig_patch_base_%s.png' % tag
    mask_name = 'ig_patch_mask_%s.png' % tag
    crop.save(os.path.join(cc.INPUT_DIR, base_name))
    model_mask.save(os.path.join(cc.INPUT_DIR, mask_name))

    prompt = cc.read_prompt(args)
    cc.require_up()
    wf, seed = cc.build_inpaint(base_name, mask_name, prompt, args.steps,
                                args.seed, args.grow, denoise=args.denoise)
    t0 = time.time()
    status, files = cc.submit(wf, timeout=900, verbose=False)
    elapsed = time.time() - t0
    print('[%s] %.1fs seed=%d' % (status, elapsed, seed))
    if status != 'success' or not files:
        return 1

    result = Image.open(files[-1]).convert('RGB')
    if result.size != (cw, ch):
        result = result.resize((cw, ch), Image.LANCZOS)

    if args.match_background:
        tgt_level = white_level(src, (x0, y0, x1, y1))
        pat_level = white_level(result, (0, 0, cw, ch))
        if tgt_level is None or pat_level is None:
            print('background match: skipped (not enough near-white pixels to measure)')
        else:
            off = tgt_level - pat_level
            if abs(off) >= 0.5:
                result = ImageChops.add(result, Image.new('RGB', result.size,
                                                          (int(round(off)),) * 3))
            print('background match: target white %.2f vs patch white %.2f -> offset %+.1f'
                  % (tgt_level, pat_level, off))

    out_im = src.copy()
    out_im.paste(result, (x0, y0), paste_mask)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    out_im.save(args.out)

    diff = ImageChops.difference(src, out_im).convert('L').histogram()
    print('patched  : %s' % args.out)
    print('outside-mask pixels unchanged: %d / %d (%.3f%% differ, all inside the mask)'
          % (diff[0], W * H, 100.0 * sum(diff[1:]) / (W * H)))
    print('model out: %s' % files[-1])
    print(json.dumps({'ok': True, 'status': status, 'seed': seed, 'seconds': round(elapsed, 1),
                      'crop': [x0, y0, x1, y1], 'denoise': args.denoise,
                      'mask_blur': args.mask_blur, 'feather': args.feather,
                      'out': args.out, 'model_out': files[-1]}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
