"""Terminate every RunPod pod this project owns. The money-safety backstop.

A pod bills until it is deleted, so a crashed orchestrator must never be able to
leave one running silently. This is the manual kill switch, independent of the
worker and of the database — it asks RunPod directly which pods carry our name
prefix and deletes them.

    python -m webapp.cloud.reaper            # list what we own, then ask
    python -m webapp.cloud.reaper --yes      # terminate them all, no prompt
    python -m webapp.cloud.reaper --dry-run  # list only

Safe to run at any time: if nothing is ours, it does nothing.
"""

from __future__ import annotations

import argparse
import sys

from .runpod_api import POD_NAME_PREFIX, RunpodClient, RunpodError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m webapp.cloud.reaper",
                                 description="terminate every pod this project owns")
    ap.add_argument("--yes", action="store_true", help="terminate without prompting")
    ap.add_argument("--dry-run", action="store_true", help="list only, terminate nothing")
    args = ap.parse_args(argv)

    try:
        client = RunpodClient()
        bal = client.balance()
        pods = client.our_pods()
    except RunpodError as exc:
        print(f"error talking to RunPod: {exc}", file=sys.stderr)
        return 1

    print(f"balance ${bal['balance']:.2f}   current spend ${bal['spend_per_hr']:.2f}/h")
    if not pods:
        print(f"no live pods with prefix {POD_NAME_PREFIX!r} — nothing to reap")
        return 0

    print(f"\n{len(pods)} pod(s) owned by this project:")
    for p in pods:
        print(f"  {p['id']}  {p.get('name'):30} {p.get('desiredStatus'):10} ${p.get('costPerHr', 0)}/h")

    if args.dry_run:
        print("\n--dry-run: nothing terminated")
        return 0

    if not args.yes:
        try:
            reply = input(f"\nterminate all {len(pods)} pod(s)? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in {"y", "yes"}:
            print("aborted — no pods terminated")
            return 1

    failed = 0
    for p in pods:
        try:
            client.terminate_pod(p["id"])
            print(f"terminated {p['id']}")
        except RunpodError as exc:
            failed += 1
            print(f"FAILED to terminate {p['id']}: {exc}", file=sys.stderr)

    if failed:
        print(f"\n{failed} pod(s) may STILL BE BILLING — check https://console.runpod.io/pods",
              file=sys.stderr)
        return 1
    print(f"\nall {len(pods)} pod(s) terminated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
