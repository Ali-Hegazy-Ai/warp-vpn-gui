#!/usr/bin/env bash
# Simple launcher for the WARP VPN GUI.
# Usage: ./launcher.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Prefer the bundled PyInstaller binary if it exists; fall back to the Python script.
if [[ -x "$APP_DIR/dist/warp-gui" ]]; then
    exec "$APP_DIR/dist/warp-gui"
elif [[ -x "$APP_DIR/dist/warp_gui/warp_gui" ]]; then
    exec "$APP_DIR/dist/warp_gui/warp_gui"
else
    exec python3 "$APP_DIR/warp_gui.py"
fi
