#!/usr/bin/env python3
"""Upscale a single image or a directory of PNG frames with a Real-ESRGAN / ESRGAN
model, loaded via spandrel so it runs on current PyTorch (Blackwell sm_120).

With 96GB VRAM we never need to tile these SD-sized frames, so the loop stays simple.
"""
import argparse, glob, os, sys, time
import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader


def load_model(path: str):
    model = ModelLoader().load_from_file(path)
    net = model.cuda().eval().half()  # fp16: plenty accurate for upscaling, ~2x faster
    return net, model.scale


@torch.inference_mode()
def upscale(net, img: Image.Image) -> Image.Image:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).cuda().half()
    out = net(t).float().squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8))


def collect(inp):
    if os.path.isdir(inp):
        return sorted(glob.glob(os.path.join(inp, "*.png")))
    return [inp]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", required=True)
    ap.add_argument("-i", "--input", required=True, help="image file or dir of PNGs")
    ap.add_argument("-o", "--output", required=True, help="output file or dir")
    args = ap.parse_args()

    net, scale = load_model(args.model)
    print(f"loaded {os.path.basename(args.model)}  scale={scale}x", flush=True)

    files = collect(args.input)
    out_is_dir = os.path.isdir(args.input) or args.output.endswith("/")
    if out_is_dir:
        os.makedirs(args.output, exist_ok=True)

    t0 = time.time()
    for n, f in enumerate(files, 1):
        img = Image.open(f)
        res = upscale(net, img)
        if out_is_dir:
            dst = os.path.join(args.output, os.path.basename(f))
        else:
            dst = args.output
        res.save(dst)
        if n == 1 or n % 25 == 0 or n == len(files):
            fps = n / (time.time() - t0)
            print(f"  {n}/{len(files)}  {img.size}->{res.size}  {fps:.2f} img/s", flush=True)
    print(f"done in {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
