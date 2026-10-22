#!/usr/bin/env bash
# Installs, updates, or uninstalls the Portkey Image Proxy launchd service and menu-bar icon on macOS.
# Usage:
#   ./install_macos.sh            # install or update
#   ./install_macos.sh uninstall  # stop and remove LaunchAgents
set -euo pipefail

ACTION="${1:-install}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SUPPORT="$HOME/Library/Application Support/PortkeyImageProxy"
VENV_DIR="$APP_SUPPORT/venv"
LOG_DIR="$HOME/Library/Logs/PortkeyImageProxy"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

PROXY_LABEL="com.portkey.imageproxy"
MENUBAR_LABEL="com.portkey.imageproxy.menubar"
PROXY_PLIST="$LAUNCH_AGENTS/$PROXY_LABEL.plist"
MENUBAR_PLIST="$LAUNCH_AGENTS/$MENUBAR_LABEL.plist"
UID_NUM="$(id -u)"

if [[ "$ACTION" == "uninstall" ]]; then
    echo "Stopping and removing LaunchAgents..."
    launchctl bootout "gui/$UID_NUM/$MENUBAR_LABEL" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/$PROXY_LABEL" 2>/dev/null || true
    rm -f "$MENUBAR_PLIST" "$PROXY_PLIST"
    rm -rf "$APP_SUPPORT"
    echo "Uninstalled PortkeyImageProxy LaunchAgents and runtime files."
    exit 0
fi

# Locate Python 3
PYTHON_BIN=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo "Error: python3 not found. Install Python 3 (e.g. 'brew install python' or from python.org)." >&2
    exit 1
fi

echo "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"
mkdir -p "$APP_SUPPORT" "$LOG_DIR" "$LAUNCH_AGENTS"

# Copy runtime scripts to ~/Library/Application Support/PortkeyImageProxy
cp "$SCRIPT_DIR/proxy.py" "$APP_SUPPORT/proxy.py"
cp "$SCRIPT_DIR/switch_gateway.py" "$APP_SUPPORT/switch_gateway.py"
cp "$SCRIPT_DIR/menubar_macos.py" "$APP_SUPPORT/menubar_macos.py"

# Create or update dedicated virtual environment for the menu-bar app (rumps + pyobjc)
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating Python virtual environment at $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing macOS menu-bar dependencies (rumps)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements-macos.txt"

# Write LaunchAgent plist for the background proxy service
cat > "$PROXY_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PROXY_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>$APP_SUPPORT/proxy.py</string>
        <string>--port</string>
        <string>8787</string>
        <string>--log-file</string>
        <string>$LOG_DIR/proxy.log</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$APP_SUPPORT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/service_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/service_stderr.log</string>
</dict>
</plist>
EOF

# Write LaunchAgent plist for the macOS menu-bar icon
cat > "$MENUBAR_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$MENUBAR_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>$APP_SUPPORT/menubar_macos.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$APP_SUPPORT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/menubar_error.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/menubar_error.log</string>
</dict>
</plist>
EOF

# Reload LaunchAgents
launchctl bootout "gui/$UID_NUM/$PROXY_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID_NUM/$MENUBAR_LABEL" 2>/dev/null || true

launchctl bootstrap "gui/$UID_NUM" "$PROXY_PLIST"
launchctl enable "gui/$UID_NUM/$PROXY_LABEL"
launchctl kickstart -k "gui/$UID_NUM/$PROXY_LABEL"

launchctl bootstrap "gui/$UID_NUM" "$MENUBAR_PLIST"
launchctl enable "gui/$UID_NUM/$MENUBAR_LABEL"
launchctl kickstart -k "gui/$UID_NUM/$MENUBAR_LABEL"

sleep 2
HEALTH="$(curl -fsS --max-time 5 http://127.0.0.1:8787/__proxy_health || echo "failed")"
echo "Service status: $PROXY_LABEL loaded, health=$HEALTH"
echo "Log file:       $LOG_DIR/proxy.log"
echo "INSTALL OK"
