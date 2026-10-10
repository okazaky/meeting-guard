#!/usr/bin/env bash
# Stop and remove the meeting-guard launchd agent.
#   ./uninstall.sh                keep script, config, secrets, tokens and logs
#   ./uninstall.sh --purge        show what --purge --yes would delete, then exit 1
#   ./uninstall.sh --purge --yes  also delete the meeting-guard data and config dirs
#                                 (only directories created by install.sh)
set -euo pipefail

LABEL="io.github.meeting-guard"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${MEETING_GUARD_HOME:-$HOME/.local/share/meeting-guard}"
CONF_DIR="$HOME/.config/meeting-guard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# shellcheck source=lib/paths.sh
. "$SRC_DIR/lib/paths.sh"

PURGE=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes) YES=1 ;;
    *) echo "usage: $0 [--purge [--yes]]" >&2; exit 2 ;;
  esac
done
if [ "$YES" = 1 ] && [ "$PURGE" = 0 ]; then
  echo "--yes is only meaningful with --purge" >&2
  exit 2
fi

revoke_note() {
  echo "To revoke Google access: https://myaccount.google.com/connections"
  echo "  (remove the OAuth client you created for meeting-guard)"
}

purge_ok() {
  # Full validation; run when listing AND again right before each delete.
  mg_check_dir "$1" purge
}

# Targets live in a bash array (never a newline-joined string).
TARGETS=()
if [ "$PURGE" = 1 ]; then
  for dir in "$APP_HOME" "$CONF_DIR"; do
    if mg_has_ctrl "$dir"; then
      mg_refuse "(path)" "contains a control character (newline, tab ...)"
      exit 1
    fi
    if [ ! -e "$dir" ] && [ ! -L "$dir" ]; then
      echo "not found (skipped): $dir"
      continue
    fi
    purge_ok "$dir" || exit 1
    TARGETS+=("${dir%/}")
  done
  if [ "$YES" != 1 ]; then
    echo "--purge would delete:"
    for dir in ${TARGETS[@]+"${TARGETS[@]}"}; do printf '  %s\n' "$dir"; done
    echo "  $PLIST (launchd agent)"
    echo "Re-run with --purge --yes to delete. Nothing was changed."
    exit 1
  fi
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f -- "$PLIST"
echo "stopped and removed: $PLIST"

STATUS=0
if [ "$PURGE" = 1 ]; then
  for dir in ${TARGETS[@]+"${TARGETS[@]}"}; do
    if ! purge_ok "$dir"; then
      echo "kept (failed re-check right before delete): $dir" >&2
      STATUS=1
      continue
    fi
    rm -rf -- "$dir"
    echo "purged: $dir"
  done
else
  echo "kept: $APP_HOME and $CONF_DIR (use --purge --yes to delete)"
fi
revoke_note
exit "$STATUS"
