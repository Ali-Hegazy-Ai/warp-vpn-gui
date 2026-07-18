#!/usr/bin/env bash

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_RAW="${1:-0.0.0}"
OUTPUT_PATH="${2:-$APP_DIR/dist/cloudflare-warp-vpn-gui_${VERSION_RAW}_amd64.deb}"

normalize_version() {
    local version="$1"
    version="${version#v}"
    version="${version// /}"
    version="$(printf '%s' "$version" | sed 's/[^0-9A-Za-z.+:~-]/-/g')"
    if [[ -z "$version" ]]; then
        version="0.0.0"
    fi
    printf '%s' "$version"
}

VERSION="$(normalize_version "$VERSION_RAW")"
BINARY="$APP_DIR/dist/warp-gui"
PKG_DIR="$APP_DIR/build/deb-pkg"
PKG_ROOT="$PKG_DIR/cloudflare-warp-vpn-gui_${VERSION}_amd64"
CONTROL_DIR="$PKG_ROOT/DEBIAN"
BIN_DIR="$PKG_ROOT/usr/bin"
APP_DIR_INSTALL="$PKG_ROOT/usr/share/applications"
ICON_DIR="$PKG_ROOT/usr/share/icons/hicolor/48x48/apps"

if [[ ! -x "$BINARY" ]]; then
    echo "Missing standalone binary at $BINARY. Run ./build.sh first." >&2
    exit 1
fi

rm -rf "$PKG_DIR"
mkdir -p "$CONTROL_DIR" "$BIN_DIR" "$APP_DIR_INSTALL" "$ICON_DIR"

cat > "$CONTROL_DIR/control" <<EOF
Package: cloudflare-warp-vpn-gui
Version: $VERSION
Section: net
Priority: optional
Architecture: amd64
Maintainer: Ali Hegazy <ali_hegazy_@outlook.com>
Depends: python3-tk
Recommends: cloudflare-warp
Description: Cloudflare WARP VPN GUI
 Simple desktop GUI for connecting and disconnecting Cloudflare WARP.
EOF

cp "$BINARY" "$BIN_DIR/warp-gui"
python3 "$APP_DIR/tools/gen_icon.py" "$ICON_DIR/warp-vpn.png"
sed 's|Exec=.*|Exec=/usr/bin/warp-gui|; s|Icon=.*|Icon=warp-vpn|' \
    "$APP_DIR/warp-vpn.desktop" > "$APP_DIR_INSTALL/warp-vpn.desktop"

mkdir -p "$(dirname "$OUTPUT_PATH")"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$OUTPUT_PATH"
echo "Built Debian package: $OUTPUT_PATH"
