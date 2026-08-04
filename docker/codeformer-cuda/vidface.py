#!/usr/bin/env python3
"""Temporally-stable CodeFormer face restoration for VIDEO frame sequences.

Adds two things on top of vanilla per-frame CodeFormer, for better *perceived*
quality in motion:
  1. size-gating   -- only restore faces whose box >= --min-face px; tiny/background
                      faces are left as the (already clean) upscaled input, which
                      avoids uncanny detail + flicker on distant faces.
  2. track + EMA   -- associate faces across frames by box centre; exponentially
                      blend each restored aligned 512 crop with the same track's
                      previous restored crop to damp shimmer. Motion-gated: if the
                      face moved/scaled a lot, reset (alpha=1) so fast motion never
                      ghosts.

Runs inside the codeformer-cuda image (has basicsr + facelib + weights).
Usage: vidface.py --input <frames_dir> --output <dir> [-w 0.7] [--min-face 90]
                  [--ema 0.6] [--upscale 1]
"""
import argparse, glob, math, os
import cv2, numpy as np, torch
from torchvision.transforms.functional import normalize
from basicsr.utils import img2tensor, tensor2img
from basicsr.utils.download_util import load_file_from_url
from basicsr.utils.registry import ARCH_REGISTRY
from facelib.utils.face_restoration_helper import FaceRestoreHelper

URL = 'https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('-w', '--fidelity', type=float, default=0.7)
    ap.add_argument('--min-face', type=int, default=90, help='min face box px to restore')
    ap.add_argument('--ema', type=float, default=0.6, help='temporal blend: new*ema + prev*(1-ema)')
    ap.add_argument('--upscale', type=int, default=1)
    args = ap.parse_args()
    device = 'cuda'
    os.makedirs(args.output, exist_ok=True)

    net = ARCH_REGISTRY.get('CodeFormer')(dim_embd=512, codebook_size=1024, n_head=8,
        n_layers=9, connect_list=['32', '64', '128', '256']).to(device)
    ckpt = load_file_from_url(url=URL, model_dir='weights/CodeFormer', progress=False)
    net.load_state_dict(torch.load(ckpt)['params_ema'])
    net.eval()

    helper = FaceRestoreHelper(args.upscale, face_size=512, crop_ratio=(1, 1),
        det_model='retinaface_resnet50', save_ext='png', use_parse=True, device=device)

    files = sorted(glob.glob(os.path.join(args.input, '*.[jpJP][pnPN]*[gG]')))
    tracks = []  # each: {'c':(cx,cy),'s':size,'prev':restored512 or None,'seen':frame_idx}
    n_faces_total = 0

    for fi, fp in enumerate(files):
        img = cv2.imread(fp, cv2.IMREAD_COLOR)
        helper.clean_all()
        helper.read_image(img)
        helper.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
        helper.align_warp_face()

        for idx, cropped in enumerate(helper.cropped_faces):
            box = helper.det_faces[idx]  # [x0,y0,x1,y1,score]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            size = max(box[2] - box[0], box[3] - box[1])

            if size < args.min_face:                      # size-gate: leave as upscaled bg
                helper.add_restored_face(cropped, cropped)
                continue

            # restore this face
            t = img2tensor(cropped / 255., bgr2rgb=True, float32=True)
            normalize(t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
            t = t.unsqueeze(0).to(device)
            try:
                with torch.no_grad():
                    out = net(t, w=args.fidelity, adain=True)[0]
                restored = tensor2img(out, rgb2bgr=True, min_max=(-1, 1)).astype('uint8')
                del out
            except Exception as e:
                print('  restore failed:', e, flush=True)
                restored = tensor2img(t, rgb2bgr=True, min_max=(-1, 1)).astype('uint8')
            n_faces_total += 1

            # track association (nearest centre within 0.6*size), motion-gated EMA
            best, bestd = None, 1e9
            for tr in tracks:
                d = math.hypot(cx - tr['c'][0], cy - tr['c'][1])
                if d < bestd:
                    bestd, best = d, tr
            moved_ok = best is not None and bestd < 0.6 * size and 0.75 < size / best['s'] < 1.33
            if moved_ok and best['prev'] is not None and best['seen'] == fi - 1:
                a = args.ema
                restored = (a * restored.astype(np.float32) +
                            (1 - a) * best['prev'].astype(np.float32)).clip(0, 255).astype('uint8')
                best.update(c=(cx, cy), s=size, prev=restored, seen=fi)
            else:
                if best is not None and moved_ok:
                    best.update(c=(cx, cy), s=size, prev=restored, seen=fi)
                else:
                    tracks.append({'c': (cx, cy), 's': size, 'prev': restored, 'seen': fi})

            helper.add_restored_face(restored, cropped)

        helper.get_inverse_affine(None)
        result = helper.paste_faces_to_input_image(upsample_img=img)
        cv2.imwrite(os.path.join(args.output, os.path.basename(fp).rsplit('.', 1)[0] + '.png'), result)
        if fi == 0 or (fi + 1) % 100 == 0 or fi == len(files) - 1:
            print(f'[vidface] {fi+1}/{len(files)} frames, {len(tracks)} tracks, {n_faces_total} faces restored', flush=True)

    print('[vidface] DONE', flush=True)


if __name__ == '__main__':
    main()
