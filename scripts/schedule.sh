#!/bin/bash
# Run `python -m techbridge update` automatically every day using macOS launchd.
#
#   scripts/schedule.sh install [HOUR]   # default 6 (6:00am). If the Mac is asleep, runs on wake.
#   scripts/schedule.sh status           # is it installed? last log lines
#   scripts/schedule.sh run              # trigger a run right now
#   scripts/schedule.sh uninstall
#
# Output goes to data/update.log.

set -euo pipefail

LABEL="com.techbridge.update"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$PROJECT/data/update.log"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  install)
    HOUR="${2:-6}"
    [[ -x "$PYTHON" ]] || { echo "No virtualenv at $PYTHON. See README setup."; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/data"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-u</string>
    <string>-m</string>
    <string>techbridge</string>
    <string>update</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
EOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Installed: TechBridge will update daily at ${HOUR}:00. Log: $LOG"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed the daily TechBridge update."
    ;;
  run)
    launchctl kickstart "$DOMAIN/$LABEL"
    echo "Started. Follow along with: tail -f \"$LOG\""
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "Installed ($PLIST)"
      launchctl print "$DOMAIN/$LABEL" | grep -E "state =|last exit code" || true
    else
      echo "Not installed."
    fi
    [[ -f "$LOG" ]] && { echo "--- last lines of $LOG ---"; tail -n 8 "$LOG"; }
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
