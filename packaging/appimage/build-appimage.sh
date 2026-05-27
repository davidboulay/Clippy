#!/usr/bin/env bash
# Build a Clippy AppImage (EXPERIMENTAL).
#
# Bundling a Python + GTK3 + GObject-introspection app is inherently fiddly:
# this script assembles an AppDir that carries Python, the gi bindings, the GTK3
# typelibs, gtk-layer-shell, AppIndicator and wl-clipboard from the BUILD HOST,
# then uses linuxdeploy + the GTK plugin to pull in shared-library deps.
#
# Run it on a machine that already has Clippy's runtime deps installed (the same
# packages the .deb depends on). Downloads linuxdeploy/appimagetool from GitHub.
#
# For most users the .deb (Ubuntu/Pop!_OS) or Flatpak (other distros) is more
# reliable than an AppImage for this kind of app.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' "$REPO/clippy/__init__.py")"
WORK="$(mktemp -d)"
APPDIR="$WORK/AppDir"
DIST="$REPO/dist"; mkdir -p "$DIST"
ARCH="$(uname -m)"
PYVER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
GIR_DIR="/usr/lib/${ARCH}-linux-gnu/girepository-1.0"

echo "==> AppDir for clippy $VERSION (python $PYVER, $ARCH)"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/clippy" \
         "$APPDIR/usr/lib/girepository-1.0" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# --- app + python bits ----------------------------------------------------
cp -r "$REPO/clippy" "$APPDIR/usr/lib/clippy/"
find "$APPDIR/usr/lib/clippy" -name '__pycache__' -type d -prune -exec rm -rf {} + || true
cp "$(readlink -f "$(command -v python3)")" "$APPDIR/usr/bin/python3"

# gi bindings (from dist-packages) + GI typelibs we rely on
PYSITE="$APPDIR/usr/lib/python${PYVER}/site-packages"
mkdir -p "$PYSITE"
cp -r /usr/lib/python3/dist-packages/gi "$PYSITE/" 2>/dev/null \
   || cp -r /usr/lib/python3/dist-packages/gi "$PYSITE/"
for tl in Gtk-3.0 Gdk-3.0 GdkPixbuf-2.0 Gio-2.0 GLib-2.0 GObject-2.0 \
          Pango-1.0 cairo-1.0 GtkLayerShell-0.1 AyatanaAppIndicator3-0.1 \
          Atk-1.0 HarfBuzz-0.0 freetype2-2.0 GModule-2.0; do
    [ -f "$GIR_DIR/$tl.typelib" ] && cp "$GIR_DIR/$tl.typelib" \
        "$APPDIR/usr/lib/girepository-1.0/" || true
done

# wl-clipboard binaries
for b in wl-copy wl-paste; do
    p="$(command -v $b || true)"; [ -n "$p" ] && cp "$p" "$APPDIR/usr/bin/" || \
        echo "    WARN: $b not found on host (clipboard won't work in the AppImage)"
done

# launcher, desktop, icon
cat > "$APPDIR/usr/bin/clippy" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")/.."
export PYTHONPATH="$HERE/lib/clippy:$HERE/lib/python*/site-packages:$PYTHONPATH"
exec "$HERE/bin/python3" -m clippy "$@"
EOF
chmod +x "$APPDIR/usr/bin/clippy"

cp "$REPO/packaging/flatpak/com.lojel.Clippy.desktop" \
   "$APPDIR/usr/share/applications/clippy.desktop"
sed -i 's/^Icon=.*/Icon=clippy/' "$APPDIR/usr/share/applications/clippy.desktop"
cp "$APPDIR/usr/share/applications/clippy.desktop" "$APPDIR/clippy.desktop"
python3 - "$REPO/clippy/icons/clippy.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/clippy.png" <<'PY'
import sys, gi; gi.require_version("GdkPixbuf","2.0")
from gi.repository import GdkPixbuf
GdkPixbuf.Pixbuf.new_from_file_at_scale(sys.argv[1],256,256,True).savev(sys.argv[2],"png",[],[])
PY
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/clippy.png" "$APPDIR/clippy.png"

# AppRun sets up the bundled GI/GTK environment
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
export LD_LIBRARY_PATH="$HERE/usr/lib:$LD_LIBRARY_PATH"
export GI_TYPELIB_PATH="$HERE/usr/lib/girepository-1.0:$GI_TYPELIB_PATH"
export PYTHONPATH="$HERE/usr/lib/clippy:$(echo "$HERE"/usr/lib/python*/site-packages):$PYTHONPATH"
exec "$HERE/usr/bin/python3" -m clippy "$@"
EOF
chmod +x "$APPDIR/AppRun"

# --- tools ----------------------------------------------------------------
cd "$WORK"
dl() { wget -q "$1" -O "$2" && chmod +x "$2"; }
echo "==> Fetching linuxdeploy + GTK plugin + appimagetool"
dl "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-${ARCH}.AppImage" linuxdeploy
dl "https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh" linuxdeploy-plugin-gtk
dl "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" appimagetool

echo "==> Bundling shared libraries (linuxdeploy + GTK plugin)"
./linuxdeploy --appdir "$APPDIR" --plugin gtk \
    -d "$APPDIR/usr/share/applications/clippy.desktop" \
    -i "$APPDIR/clippy.png" || true

echo "==> Packaging AppImage"
OUT="$DIST/Clippy-${VERSION}-${ARCH}.AppImage"
./appimagetool "$APPDIR" "$OUT"
echo "==> Built $OUT"
