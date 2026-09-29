# Mo & Anisha DVD — conversion runbook and ETA

How to restore the Mo & Anisha wedding DVD (`source/mo_dvd1.iso`, catalog
disc 4). This follows the Gulfraz disc-2 run of 2026-09-25, which used the
same cloud stack. Source analysis is in
[`MO_DISC_INVESTIGATION_2026-09-25.md`](MO_DISC_INVESTIGATION_2026-09-25.md).

## At a glance

| | |
|---|---|
| Footage | 4 titles: 10.4 + 46.0 + 16.5 + 36.8 min = **109.7 min** |
| Format | PAL 25i, top-field-first (verified), 704×576 4:3 |
| Code changes needed | **None.** TFF is already configured for this disc and was proven on a 15 s test clip |
| Work | **12 segments**, 329,194 output frames, **441 durable units** |
| Cost | **~$270–380** of RunPod. **Top up to ~$400** first; the balance was $10.37 after Gulfraz |
| Time, 8 pods | **~18–20 h** expected (range 14–24 h) |
| Time, 12–16 pods | ~10–13 h, if RunPod has the stock |
| Time, local GPU only | ~4 days, $0 |

Segments of about 10 minutes give 40 units each. Short ones (46:01, 16:29, 36:50 endings) take roughly an hour, full ones 1.5–2 h at 8 pods. Every segment boundary is a safe place to stop.

### Where the ETA comes from

The estimate uses what was measured on Gulfraz disc-2 with the warm engine on:

- **Wall-clock:** 1.8 min per unit at a clean 8 pods, and about 2.7 min per unit including create-lottery shortfalls, segment boundaries and the occasional bad host. 441 × 1.8 ≈ 13 h is the best case; 441 × 2.7 ≈ 20 h is the realistic case.
- **Cost:** $0.61–0.86 per unit, all-in.

What makes it fast or slow is mostly RunPod stock. On 2026-09-25 only 3–7 of 8 pod creates succeeded at each bring-up. Keep-alive now carries pods across the 11 segment boundaries, so each boundary only has to win pods that died in the previous segment.

## Before you start (about 15 minutes, $0)

1. **Top up RunPod to about $400.** Also set an account-level spend limit in the RunPod console as the last-resort guard.
2. **Check disk:** there must be at least 100 GiB free (the worker refuses jobs below that) plus about 10 GB for the segment in progress. Check with `df -h /`. On 2026-09-29 there were 411 GB free.
3. **Optional quality check:** open `webapp/data/outputs/restore-704adf4741bb.mkv` (the 15 s TFF test from title 2) and step frame by frame through a camera pan. Motion must move steadily in one direction. Back-and-forth stutter would mean the field order is wrong, which isn't expected.
4. **Stop the local worker, or decide to let it help.** An idle local worker grabs queued segments and restores them at about 14 h each. On Gulfraz that had to be undone by hand. Either stop it, or let it take a short segment deliberately.

## Run it

**1. Queue the 12 segments** in viewing order (title 1 → 4). Always dry-run first:

```bash
cd ~/Projects/upscale-wedding-videos
webapp/.venv/bin/python scripts/queue_segments.py --chapters 36 37 38 39           # prints the plan
webapp/.venv/bin/python scripts/queue_segments.py --chapters 36 37 38 39 --commit  # queues + start-requests
```

The script is safe to re-run: segments that are already queued are skipped. It creates one catalog slice and one job per ~10-minute window, the same shape as Gulfraz.

**2. Launch one continuous cloud worker:**

```bash
WEDDING_EXECUTOR=cloud WEDDING_DURABLE_UNITS=1 WEDDING_CLOUD_MAX_SLOTS=8 \
WEDDING_CLOUD_SPEND_CAP_USD=100 WEDDING_POD_ENGINE=1 WEDDING_CLOUD_KEEP_ALIVE=1 \
  nohup webapp/.venv/bin/python -m webapp.worker.runner \
  > webapp/data/logs/mo-cloud-worker.log 2>&1 &
```

- `WEDDING_CLOUD_SPEND_CAP_USD` is **per segment**. $100 is about 3× a segment's real cost.
- To go wider, raise `WEDDING_CLOUD_MAX_SLOTS` (up to 16). Wider doesn't cost more in total; it only finishes sooner.
- *Optional, not yet tried live:* add `WEDDING_CLOUD_IMAGE=ghcr.io/stbuser46/seedvr2-pod:v3` to use the pre-baked pod image. It skips part of each pod's setup; the 7 GB model weights still download on the pod. If the first bring-up behaves oddly, relaunch without it.

## While it runs

- **Fewer pods than asked for at bring-up** (normal): wait about 4 minutes, then `echo 1 > webapp/data/restoration_work/<job>/add_pods`. If that pod comes up, add the rest. Each segment can replace or add at most 8 pods.
- **Leave the single idle "standby" pod near a segment's end alone.** It is insurance for the last unit, and it also runs a duplicate of that unit.
- **"Engine silent for 900s (wedged)"** happens on about 1% of units. It is handled automatically: the unit re-runs the classic way and a bad host gets replaced.
- **"No new banked unit" alerts during bring-up** are false alarms. They measure from the previous segment's last unit.
- **Pod probing:** check pods mainly in the webapp. At most an occasional `nvidia-smi` over ssh with `ConnectTimeout=6`; heavy probing got the home IP rate-limited once.
- **Spend cap or low balance:** the segment pauses cleanly and keeps every banked unit. Top up, raise the cap, and relaunch; it resumes from where it stopped.

## Deliver to the NAS

As each segment completes, copy its output into
`WeddingFilm/Mo and anisha/Upscaled/`. The folder already exists on the NAS.
Use `scp`, and **leave out `/volume1`**: SFTP on this NAS starts at the share
root. `rsync` to the NAS does not work.

```bash
scp webapp/data/outputs/<public_id>.mkv \
  'yimoolla@nas.home:/Movies/WeddingFilm/Mo and anisha/Upscaled/Mo DVD - Title 2 - Segment 03 (20-30 min) - Restored HD.mkv'
```

Check that the file size on the NAS matches the local one. Once a segment is
safely on the NAS, you can delete its `webapp/data/restoration_work/<public_id>/`
folder, which is about 8–11 GB.

## Done

- Once the last segment completes, confirm `RunpodClient().our_pods()` is empty, then stop the worker with SIGTERM.
- Optionally regenerate the webapp's restored-HD proxies so the player shows the new footage.
