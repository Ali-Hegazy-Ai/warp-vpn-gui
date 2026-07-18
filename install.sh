#!/usr/bin/env bash
# One-command installer: copies desktop entry + icon, refreshes caches.
# Run:  ./install.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_SRC="$APP_DIR/warp-vpn.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/48x48/apps"
ICON_DST="$ICON_DIR/warp-vpn.png"

echo "Installing Cloudflare WARP VPN into application menu..."

# Copy desktop entry
mkdir -p "$HOME/.local/share/applications"
cp "$DESKTOP_SRC" "$HOME/.local/share/applications/"
echo "  ✓ Desktop entry: $HOME/.local/share/applications/warp-vpn.desktop"

# Copy icon
mkdir -p "$ICON_DIR"
if [ -f /usr/share/icons/gnome/48x48/devices/network-vpn.png ]; then
    cp /usr/share/icons/gnome/48x48/devices/network-vpn.png "$ICON_DST"
    echo "  ✓ Icon: $ICON_DST (from system)"
else
    python3 "$APP_DIR/tools/gen_icon.py" "$ICON_DST"
    echo "  ✓ Icon: $ICON_DST (generated)"
fi

# Refresh caches
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor/" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications/" 2>/dev/null || true

echo ""
echo "Installation complete."
echo "Launch 'Cloudflare WARP VPN' from your application menu."
echo ""
echo "To uninstall:  rm $HOME/.local/share/applications/warp-vpn.desktop && rm $ICON_DST"
