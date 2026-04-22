#!/usr/bin/env bash
# Install ipod_watcher.py as a LaunchAgent so it starts at login.
# Idempotent: safe to re-run.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LABEL="nl.tycholitjens.ipod-weekly"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PY="${SCRIPT_DIR}/.venv/bin/python"
WATCHER="${SCRIPT_DIR}/ipod_watcher.py"
LOG="${SCRIPT_DIR}/.watcher.log"

if [[ ! -x "$PY" ]]; then
    echo "error: venv python not found at $PY" >&2
    echo "  run: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [[ ! -f "$WATCHER" ]]; then
    echo "error: watcher script missing at $WATCHER" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Unload any previous version so changes take effect.
launchctl unload "$PLIST" 2>/dev/null || true

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PY}</string>
        <string>${WATCHER}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
</dict>
</plist>
EOF

launchctl load "$PLIST"
echo "installed and loaded: $PLIST"
echo "status:"
launchctl list | grep "${LABEL}" || echo "  (not running yet)"
echo
echo "to uninstall: launchctl unload \"$PLIST\" && rm \"$PLIST\""
echo "to view log:  tail -f \"$LOG\""
