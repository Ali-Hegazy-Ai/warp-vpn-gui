#!/usr/bin/env bash
# Build a standalone PyInstaller executable.
#
# Usage: ./build.sh
#
# Prerequisites:
#   python3 -m venv /tmp/warp-venv
#   /tmp/warp-venv/bin/pip install pyinstaller

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

VENV="${VENV:-/tmp/warp-venv}"
PYINSTALLER="$VENV/bin/pyinstaller"

if [[ ! -x "$PYINSTALLER" ]]; then
    echo "Creating virtual environment and installing PyInstaller..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet pyinstaller
fi

# Clean previous build artifacts
rm -rf "$APP_DIR/dist" "$APP_DIR/build" "$APP_DIR"/*.spec 2>/dev/null

echo "Building standalone executable..."
"$PYINSTALLER" --onefile --name warp-gui \
    --distpath "$APP_DIR/dist" \
    --workpath "$APP_DIR/build" \
    --specpath "$APP_DIR/build" \
    --log-level WARN \
    warp_gui.py

echo ""
echo "Done.  Standalone executable: $APP_DIR/dist/warp-gui"
echo "Run it with:  $APP_DIR/dist/warp-gui"
