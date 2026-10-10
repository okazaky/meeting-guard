#!/usr/bin/env bash
# Install meeting-guard as a per-user launchd agent (macOS).
# Never overwrites an existing config.json or secrets.env.
set -euo pipefail
umask 077

LABEL="io.github.meeting-guard"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${MEETING_GUARD_HOME:-$HOME/.local/share/meeting-guard}"
APP_HOME="${APP_HOME%/}"
CONF_DIR="$HOME/.config/meeting-guard"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"

# shellcheck source=lib/paths.sh
. "$SRC_DIR/lib/paths.sh"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "meeting-guard supports macOS only." >&2
  exit 1
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON="$(command -v python3 || true)"; fi
fi
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "python3 (3.9 or later) is required. Set PYTHON=/path/to/python3 if needed." >&2
  exit 1
fi

# Validate before creating, chmod-ing or copying anything.
mg_check_dir "$APP_HOME" install || exit 1
mg_check_dir "$CONF_DIR" any || exit 1
# chmod / cp would follow a planted symlink: refuse instead.
for p in "$APP_HOME/logs" "$PLIST" "$PLIST.bak"; do
  [ ! -L "$p" ] || { mg_refuse "$p" "is a symlink"; exit 1; }
done
if [ -e "$APP_HOME/logs" ] && [ ! -d "$APP_HOME/logs" ]; then
  mg_refuse "$APP_HOME/logs" "is not a directory"; exit 1
fi
if [ -e "$PLIST.bak" ] && [ ! -f "$PLIST.bak" ]; then
  mg_refuse "$PLIST.bak" "is not a regular file"; exit 1
fi

mkdir -p "$APP_HOME/logs" "$CONF_DIR" "$AGENT_DIR"
chmod 700 "$APP_HOME" "$APP_HOME/logs" "$CONF_DIR"
mg_write_marker "$APP_HOME" || exit 1
mg_write_marker "$CONF_DIR" || exit 1

install -m 755 "$SRC_DIR/meeting_guard.py" "$APP_HOME/meeting_guard.py"
for module in "$SRC_DIR"/mg_*.py; do
  install -m 644 "$module" "$APP_HOME/$(basename "$module")"
done

if [ ! -e "$APP_HOME/config.json" ]; then
  install -m 600 "$SRC_DIR/config.example.json" "$APP_HOME/config.json"
  echo "created: $APP_HOME/config.json"
else
  echo "kept existing: $APP_HOME/config.json"
fi

if [ ! -e "$CONF_DIR/secrets.env" ]; then
  install -m 600 "$SRC_DIR/secrets.env.example" "$CONF_DIR/secrets.env"
  echo "created: $CONF_DIR/secrets.env (push and phone call are off until you fill it in)"
else
  echo "kept existing: $CONF_DIR/secrets.env"
fi

# Build the plist with plistlib into a temp file, keep a backup, then move into place.
BAK_TMP=""
TMP_PLIST="$(mktemp "$AGENT_DIR/.meeting-guard.XXXXXX")"
trap 'rm -f -- "$TMP_PLIST" ${BAK_TMP:+"$BAK_TMP"}' EXIT
"$PYTHON" "$SRC_DIR/mg_plist.py" "$TMP_PLIST" "$LABEL" "$PYTHON" \
  "$APP_HOME/meeting_guard.py" "$APP_HOME"
plutil -lint "$TMP_PLIST" >/dev/null
chmod 644 "$TMP_PLIST"
if [ -f "$PLIST" ] && [ ! -L "$PLIST" ]; then
  # Backup goes to a fresh temp file first, then is renamed over $PLIST.bak
  # (rename replaces a path; it never writes through a symlink).
  BAK_TMP="$(mktemp "$AGENT_DIR/.meeting-guard-bak.XXXXXX")"
  cp -p "$PLIST" "$BAK_TMP"
  mv -f "$BAK_TMP" "$PLIST.bak"
  BAK_TMP=""
  echo "backup: $PLIST.bak"
fi
mv -f "$TMP_PLIST" "$PLIST"
trap - EXIT

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "loaded: $DOMAIN/$LABEL"

SUGGEST="mg-$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
cat <<EOF

Next steps:
  1. Save your Google OAuth client (Desktop app) JSON, then restrict it:
       $CONF_DIR/gcp-oauth.keys.json
       chmod 600 $CONF_DIR/gcp-oauth.keys.json
  2. Authorize:   $PYTHON $APP_HOME/meeting_guard.py --auth
  3. Verify:      $PYTHON $APP_HOME/meeting_guard.py --check
  4. Try it:      $PYTHON $APP_HOME/meeting_guard.py --simulate
  5. (optional) Push notifications are off. To enable, set in $CONF_DIR/secrets.env
       NTFY_TOPIC=$SUGGEST
     (a freshly generated random topic; treat it like a password)
  Logs: $APP_HOME/logs/guard.log
EOF
