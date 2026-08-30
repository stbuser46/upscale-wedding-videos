#!/usr/bin/env bash
# Install the wedding web + worker as systemd USER services and retire the
# @reboot cron launcher. Uses `systemctl --user` (linger is enabled for this
# user, so the services survive logout/reboot) — no sudo required.
#
# This is a live cutover: it stops the nohup-launched gunicorn/worker and any
# running job's GPU container is NOT touched by this script, but the worker it
# stops will requeue an interrupted job on next start. Run it when the GPU is
# idle. Reversible with uninstall.sh.
set -euo pipefail

PROJ=/home/yacoob/Projects/upscale-wedding-videos
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Installing unit files to $UNIT_DIR"
mkdir -p "$UNIT_DIR"
cp "$PROJ/deploy/systemd/wedding-web.service" "$UNIT_DIR/"
cp "$PROJ/deploy/systemd/wedding-worker.service" "$UNIT_DIR/"

echo "==> Removing @reboot start-all.sh cron entry (if present)"
if crontab -l 2>/dev/null | grep -q 'start-all.sh'; then
  # `|| true`: when start-all.sh is the only line, grep -v emits nothing and
  # exits 1, which would trip `set -e` — we still want the (empty) result piped.
  { crontab -l 2>/dev/null | grep -v 'start-all.sh' || true; } | crontab -
  echo "    cron entry removed"
else
  echo "    no matching cron entry"
fi

echo "==> Stopping nohup-launched services (if any) to free port 8093 / GPU lock"
pkill -f 'webapp\.worker\.runner' 2>/dev/null && echo "    stopped nohup worker" || echo "    no nohup worker"
# Stop the nohup gunicorn master (leaves systemd to take over the port).
if ss -ltn 2>/dev/null | grep -q '127.0.0.1:8093'; then
  pkill -f 'gunicorn.*webapp.server.app' 2>/dev/null || true
  for _ in $(seq 1 10); do ss -ltn 2>/dev/null | grep -q '127.0.0.1:8093' || break; sleep 0.5; done
  echo "    stopped nohup gunicorn"
fi

echo "==> Reloading and enabling user services"
systemctl --user daemon-reload
systemctl --user enable --now wedding-web.service
systemctl --user enable --now wedding-worker.service

echo "==> Status"
systemctl --user --no-pager --lines=0 status wedding-web.service wedding-worker.service || true
echo "Done. Follow logs with:"
echo "  journalctl --user -u wedding-worker.service -f"
echo "  journalctl --user -u wedding-web.service -f"
