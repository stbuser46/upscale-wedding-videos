#!/usr/bin/env python
"""Test fixture: a local stand-in that speaks the EXACT pod_engine.py protocol
(see docker/seedvr2-pod/pod_engine.py) with scriptable behaviors, so PodEngine
can be unit-tested against a real subprocess without a pod, ssh, or money.

Usage:  python fake_pod_engine.py <mode>

Modes (all print the real protocol's "ENGINE READY pid=<pid>" except
no-ready, which never gets that far):
    happy         READY; each request logs a few CLI-style lines, then
                  "ENGINE PID <pid>" and "ENGINE DONE 754". Serves any
                  number of requests.
    slow          READY; each request prints one line then goes silent for
                  30 s before DONE — drives stall/timeout/abort kills when the
                  test passes a short stall_s / max_run_s / an abort flag.
    err           READY; each request replies "ENGINE ERR RuntimeError: boom"
                  and stays alive for the next request.
    die           READY; the first request logs one line then the process
                  exits abruptly (no DONE/ERR) — sudden-death detection.
    garbage       READY; each request replies "ENGINE DONE not-a-number" —
                  protocol garbage where the frame count belongs.
    no-ready      Never prints READY (sleeps) — READY-timeout handling.

NOT named test_*.py on purpose: unittest discovery must not import it.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "happy"
    if mode == "no-ready":
        time.sleep(60)
        return
    print(f"ENGINE READY pid={os.getpid()}", flush=True)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "EXIT":
            break
        try:
            argv = json.loads(line)
        except ValueError:
            print("ENGINE ERR bad request line", flush=True)
            continue
        print(f"[fake] request: {len(argv)} args, input={argv[0] if argv else '?'}",
              flush=True)
        if mode == "happy":
            print("Chunk 1/6: batch 129 frames", flush=True)
            print("Written 754/754 frames", flush=True)
            print(f"ENGINE PID {os.getpid()}", flush=True)
            print("ENGINE DONE 754", flush=True)
        elif mode == "slow":
            print("Chunk 1/6: warming up", flush=True)
            time.sleep(30)
            print("ENGINE DONE 754", flush=True)
        elif mode == "err":
            print("ENGINE ERR RuntimeError: boom", flush=True)
        elif mode == "die":
            print("about to crash", flush=True)
            sys.exit(3)
        elif mode == "garbage":
            print("ENGINE DONE not-a-number", flush=True)
        else:
            print(f"ENGINE ERR unknown fake mode {mode}", flush=True)
    print("ENGINE EXITING", flush=True)


if __name__ == "__main__":
    main()
