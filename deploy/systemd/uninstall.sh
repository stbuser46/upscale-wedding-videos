#!/usr/bin/env bash
# Roll back to the @reboot cron launcher: stop+disable the user services and
# restore the crontab entry. Does not delete unit files' project copies.
set -euo pipefail

PROJ=/home/yacoob/Projects/upscale-wedding-videos
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Stopping and disabling user services"
systemctl --user disable --now wedding-reaper.timer 2>/dev/null || true
systemctl --user disable --now wedding-worker.service 2>/dev/null || true
systemctl --user disable --now wedding-web.service 2>/dev/null || true
rm -f "$UNIT_DIR/wedding-web.service" "$UNIT_DIR/wedding-worker.service" \
      "$UNIT_DIR/wedding-reaper.service" "$UNIT_DIR/wedding-reaper.timer"
systemctl --user daemon-reload

echo "==> Restoring @reboot cron entry (if missing)"
LINE="@reboot sleep 30 && $PROJ/webapp/data/start-all.sh >> $PROJ/webapp/data/logs/reboot.log 2>&1"
if crontab -l 2>/dev/null | grep -q 'start-all.sh'; then
  echo "    cron entry already present"
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "    cron entry restored"
fi
echo "==> Bringing services back up via start-all.sh"
"$PROJ/webapp/data/start-all.sh"
echo "Done."
