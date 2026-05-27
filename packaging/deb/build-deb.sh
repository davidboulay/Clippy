#!/usr/bin/env bash
# Build a Clippy .deb (Architecture: all). No network or sudo required.
#   ./packaging/deb/build-deb.sh   ->   dist/clippy_<version>_all.deb
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' "$REPO/clippy/__init__.py")"
PKG="clippy"
ARCH="all"
STAGE="$(mktemp -d)/${PKG}_${VERSION}_${ARCH}"
DIST="$REPO/dist"
mkdir -p "$DIST"

echo "==> Building $PKG $VERSION"

# --- layout ---------------------------------------------------------------
SITE="$STAGE/usr/lib/python3/dist-packages"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$SITE" \
         "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/$PKG"

# Python package (sans caches)
cp -r "$REPO/clippy" "$SITE/clippy"
find "$SITE/clippy" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SITE/clippy" -name '*.pyc' -delete 2>/dev/null || true

# Launcher
cat > "$STAGE/usr/bin/clippy" <<'EOF'
#!/bin/sh
exec python3 -m clippy "$@"
EOF
chmod 0755 "$STAGE/usr/bin/clippy"

# Icons into the hicolor theme (scaled from the bundled 512px source)
python3 - "$REPO/clippy/icons/clippy.png" "$STAGE/usr/share/icons/hicolor" <<'PY'
import os, sys, gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
src, base = sys.argv[1], sys.argv[2]
for s in (16, 22, 24, 32, 48, 64, 128, 256, 512):
    d = os.path.join(base, f"{s}x{s}", "apps"); os.makedirs(d, exist_ok=True)
    GdkPixbuf.Pixbuf.new_from_file_at_scale(src, s, s, True).savev(
        os.path.join(d, "clippy.png"), "png", [], [])
print("    icons generated")
PY

# Desktop entry (app list; not auto-pinned to the dock)
cat > "$STAGE/usr/share/applications/clippy.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Clippy
GenericName=Clipboard Manager
Comment=Show your clipboard history
Exec=clippy toggle
Icon=clippy
Terminal=false
Categories=Utility;GTK;
Keywords=clipboard;history;paste;copy;
StartupNotify=false
Actions=Settings;

[Desktop Action Settings]
Name=Settings
Exec=clippy settings
EOF

cp "$REPO/README.md" "$STAGE/usr/share/doc/$PKG/README.md" 2>/dev/null || true

# --- control + maintainer scripts ----------------------------------------
INSTALLED_KB="$(du -ks "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-gtklayershell-0.1, libgtk-layer-shell0, gir1.2-ayatanaappindicator3-0.1, libayatana-appindicator3-1, wl-clipboard
Recommends: pipewire-bin | pulseaudio-utils
Maintainer: David Boulay <david.boulay@lojel.com>
Installed-Size: $INSTALLED_KB
Description: Clipboard history panel for Wayland (COSMIC/wlroots)
 Clippy shows a Paste-style strip of recent clipboard items (text and images)
 at the bottom of the screen, toggled by a global shortcut. It lives in the
 system tray, follows the COSMIC light/dark theme, and supports pinning,
 search, history retention, and paste-as-plain-text.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
    fi
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postrm"

# --- build ----------------------------------------------------------------
OUT="$DIST/${PKG}_${VERSION}_${ARCH}.deb"
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$OUT" >/dev/null
echo "==> Built $OUT"
echo
dpkg-deb --info "$OUT" | sed 's/^/    /'
echo "    --- contents (top-level) ---"
dpkg-deb --contents "$OUT" | awk '{print $6}' | grep -E '^\./(usr/bin|usr/share/applications|usr/lib/python3/dist-packages/clippy/[^/]*$)' | head -20 | sed 's/^/    /'
