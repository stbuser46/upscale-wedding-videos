"""Terminate every RunPod pod this project owns. The money-safety backstop.

A pod bills until it is deleted, so a crashed orchestrator must never be able to
leave one running silently. This is the kill switch, independent of the worker
and of the database — it asks RunPod directly which pods carry our name prefix
and deletes them.

    python -m webapp.cloud.reaper                 # list what we own, then ask
    python -m webapp.cloud.reaper --yes           # terminate them all, no prompt
    python -m webapp.cloud.reaper --dry-run       # list only
    python -m webapp.cloud.reaper --yes --loop 300  # backstop daemon: sweep every 5 min

The listing/deletion path is deliberately DECOUPLED from the balance query: a
GraphQL balance() failure must never prevent the REST pod delete (killing pods
is the money-safety job; showing a balance is cosmetic). Install the --loop form
as a systemd timer on an independent host so a SIGKILL, power loss, or wedged
shutdown can't leave pods billing indefinitely.

Safe to run at any time: if nothing is ours, it does nothing.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from .runpod_api import POD_NAME_PREFIX, RunpodClient, RunpodError


def _pod_age_hours(pod: dict) -> float | None:
    """Best-effort pod age in hours from whatever creation timestamp RunPod
    exposes. None when it can't be determined (field missing/unparseable)."""
    for key in ("createdAt", "lastStartedAt", "lastStatusChange", "createdOn"):
        raw = pod.get(key)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0)
        except (ValueError, TypeError):
            continue
    return None


def _print_balance(client: RunpodClient) -> None:
    """Best-effort balance line. NEVER fatal — decoupled from the kill path."""
    try:
        bal = client.balance()
        print(f"balance ${bal['balance']:.2f}   current spend ${bal['spend_per_hr']:.2f}/h")
    except RunpodError as exc:
        print(f"(balance unavailable: {exc})", file=sys.stderr)


def _sweep(client: RunpodClient, *, dry_run: bool, assume_yes: bool, min_age_hours: float = 0.0) -> int:
    """One reap pass. Returns 0 if nothing is (or remains) billing, else 1.

    `min_age_hours > 0` targets ONLY pods older than that — the safe mode for an
    automatic timer: a healthy in-progress fleet (pods minutes to a couple of
    hours old) is left alone, while a genuine orphan left by a dead worker is
    reaped once it ages past the threshold. Pods whose age can't be read are
    NEVER auto-killed in age mode (avoids nuking a healthy run on a missing
    timestamp); use `--max-age-hours 0` (the manual default) to kill regardless."""
    try:
        pods = client.our_pods()
    except RunpodError as exc:
        # Could not even list — cannot confirm the fleet is dead. Fail LOUD so a
        # timer keeps retrying rather than reporting a false all-clear.
        print(f"error listing pods: {exc}", file=sys.stderr)
        return 1

    if not pods:
        print(f"no live pods with prefix {POD_NAME_PREFIX!r} — nothing to reap")
        return 0

    print(f"{len(pods)} pod(s) owned by this project:")
    for p in pods:
        age = _pod_age_hours(p)
        age_str = f"{age:.1f}h" if age is not None else "age?"
        print(f"  {p['id']}  {str(p.get('name')):30} {str(p.get('desiredStatus')):10} "
              f"${p.get('costPerHr', 0)}/h  {age_str}")

    if min_age_hours > 0:
        kept = [p for p in pods if (_pod_age_hours(p) or 0.0) < min_age_hours]
        pods = [p for p in pods if (_pod_age_hours(p) or 0.0) >= min_age_hours]
        if kept:
            print(f"leaving {len(kept)} pod(s) younger than {min_age_hours:.1f}h "
                  f"(a healthy run, or age unknown) untouched")
        if not pods:
            print(f"no pod older than {min_age_hours:.1f}h — nothing to reap")
            return 0

    if dry_run:
        print("--dry-run: nothing terminated")
        return 1  # still-live pods exist

    if not assume_yes:
        try:
            reply = input(f"terminate all {len(pods)} pod(s)? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in {"y", "yes"}:
            print("aborted — no pods terminated")
            return 1

    failed = 0
    for p in pods:
        try:
            client.terminate_pod(p["id"])  # True or raises (confirmed gone only)
            print(f"terminated {p['id']}")
        except RunpodError as exc:
            failed += 1
            print(f"FAILED to terminate {p['id']}: {exc}", file=sys.stderr)

    if failed:
        print(f"{failed} pod(s) may STILL BE BILLING — check https://console.runpod.io/pods",
              file=sys.stderr)
        return 1
    print(f"all {len(pods)} pod(s) terminated")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m webapp.cloud.reaper",
                                 description="terminate every pod this project owns")
    ap.add_argument("--yes", action="store_true", help="terminate without prompting")
    ap.add_argument("--dry-run", action="store_true", help="list only, terminate nothing")
    ap.add_argument("--loop", type=float, metavar="SECONDS", default=0.0,
                    help="run forever, sweeping every SECONDS (for a systemd timer/daemon)")
    ap.add_argument("--max-age-hours", type=float, metavar="H", default=0.0,
                    help="only terminate pods older than H hours (safe for a timer alongside "
                         "healthy runs); 0 = kill all we own (the manual default)")
    args = ap.parse_args(argv)

    try:
        client = RunpodClient()
    except RunpodError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.loop > 0:
        # Backstop daemon: --loop implies non-interactive. Keep going regardless
        # of transient failures; a single sweep error must not kill the guard.
        print(f"reaper loop: sweeping every {args.loop:.0f}s "
              f"(age filter {args.max_age_hours:.1f}h, Ctrl-C to stop)")
        while True:
            _print_balance(client)
            try:
                _sweep(client, dry_run=args.dry_run, assume_yes=True, min_age_hours=args.max_age_hours)
            except Exception as exc:  # never let the backstop die
                print(f"sweep error (continuing): {exc}", file=sys.stderr)
            time.sleep(args.loop)

    _print_balance(client)
    return _sweep(client, dry_run=args.dry_run, assume_yes=args.yes, min_age_hours=args.max_age_hours)


if __name__ == "__main__":
    raise SystemExit(main())
