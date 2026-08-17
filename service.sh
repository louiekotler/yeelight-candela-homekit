#!/bin/bash
# Install / control the bridge as a LaunchAgent.
#
#   ./service.sh install     load it and start at login
#   ./service.sh uninstall   stop it and remove
#   ./service.sh stop        free the lamps' BLE connections
#   ./service.sh start
#   ./service.sh restart
#   ./service.sh status
#   ./service.sh log         follow the log

set -euo pipefail

LABEL="com.louiekotler.candela-homekit"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/candela-homekit.log"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC" "$DEST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$DEST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "Installed. Follow the log with: $0 log"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "Uninstalled."
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "Stopped. Start again with: $0 start"
    ;;
  start)
    launchctl bootstrap "$DOMAIN" "$DEST"
    echo "Started."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "Restarted."
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "Not loaded."
    ;;
  log)
    touch "$LOG"; tail -f "$LOG"
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
