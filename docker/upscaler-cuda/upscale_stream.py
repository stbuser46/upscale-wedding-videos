#!/usr/bin/env python3
"""Streaming upscaler: raw rgb24 frames on stdin -> upscaled raw rgb24 on stdout.

Threaded so the GPU never waits on I/O:
  reader thread  -> fills a queue of frame-batches from stdin
  main thread    -> GPU upscale (the only CUDA user)
  writer thread  -> drains upscaled bytes to stdout (absorbs encoder backpressure)

Sits in an ffmpeg pipe (all inside one container for fast OS pipes):
  ffmpeg ...deinterlace... -f rawvideo -pix_fmt rgb24 - \
    | upscale_stream.py --width 768 --height 576 --model M.pth --batch 16 \
    | ffmpeg -f rawvideo -pix_fmt rgb24 -s WxH -r 50 -i - ...nvenc...
"""
import argparse, queue, sys, threading, time
import numpy as np
import torch
from spandrel import ModelLoader


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return bytes(buf) if buf else None
        buf += chunk
    return bytes(buf)


def reader(stdin, frame_bytes, H, W, batch, q):
    frames = []
    while True:
        raw = read_exact(stdin, frame_bytes)
        if raw is None or len(raw) < frame_bytes:
            break
        frames.append(np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3))
        if len(frames) >= batch:
            q.put(frames)
            frames = []
    if frames:
        q.put(frames)
    q.put(None)  # sentinel


def writer(stdout, q):
    # Receives numpy arrays (already on CPU) and does the byte-serialization here,
    # off the GPU critical path, overlapping with the next batch's compute.
    while True:
        item = q.get()
        if item is None:
            break
        stdout.write(memoryview(item))
    stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    md = ModelLoader().load_from_file(args.model)
    net = md.cuda().eval().half()
    scale = md.scale
    torch.backends.cudnn.benchmark = True

    W, H = args.width, args.height
    frame_bytes = W * H * 3
    in_q = queue.Queue(maxsize=4)
    out_q = queue.Queue(maxsize=4)

    rt = threading.Thread(target=reader, args=(sys.stdin.buffer, frame_bytes, H, W, args.batch, in_q), daemon=True)
    wt = threading.Thread(target=writer, args=(sys.stdout.buffer, out_q), daemon=True)
    rt.start(); wt.start()

    n_done = 0
    t0 = time.time()
    while True:
        frames = in_q.get()
        if frames is None:
            break
        arr = np.stack(frames)
        t = torch.from_numpy(arr).cuda().permute(0, 3, 1, 2).half().div_(255.0)
        with torch.inference_mode():
            out = net(t).clamp_(0, 1).mul_(255.0).round_()
            out = out.permute(0, 2, 3, 1).to(torch.uint8).contiguous()
        # D2H copy into pinned memory, then hand the contiguous CPU array to the
        # writer thread which serializes+writes it while the GPU starts the next batch.
        cpu = out.to("cpu", non_blocking=True)
        torch.cuda.synchronize()
        out_q.put(cpu.numpy())
        n_done += len(frames)
        if n_done % (args.batch * 20) < args.batch:
            fps = n_done / (time.time() - t0)
            print(f"[upscale] {n_done} frames  {fps:.1f} fps", file=sys.stderr, flush=True)

    out_q.put(None)
    wt.join()
    dt = time.time() - t0
    print(f"[upscale] DONE {n_done} frames in {dt:.1f}s  ({n_done/max(dt,1e-9):.1f} fps)  out={W*scale}x{H*scale}",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
