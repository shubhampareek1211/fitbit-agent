#!/usr/bin/env bash
# Install (or remove) the twice-daily refresh on this Mac: 11:00 and 23:00 local time, via launchd.
#   scripts/install_schedule.sh            install
#   scripts/install_schedule.sh remove     remove
# Each run executes build/refresh.py, which publishes the snapshot to Cloud Storage; the live app picks it up
# within ten minutes. If the Mac is asleep at the scheduled time, launchd runs the job when it wakes.
# Output goes to logs/refresh.log.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUCKET="${PULSE_BUCKET:-pulse-snapshots-phonic-weaver-475017-n1}"
LABEL=com.pulse.refresh
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "remove" ]]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi
mkdir -p "$REPO/logs" "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
    <string>\$HOME/.local/bin/uv</string><string>run</string><string>build/refresh.py</string>
    <string>--bucket</string><string>$BUCKET</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>\$HOME/.local/bin:/usr/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$REPO/logs/refresh.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/refresh.log</string>
</dict></plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL: runs at 11:00 and 23:00, log in $REPO/logs/refresh.log"
