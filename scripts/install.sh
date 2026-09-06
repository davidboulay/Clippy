#!/usr/bin/env bash
# Installs system dependencies, a launcher, autostart, and the icon, then
# starts the Clippy daemon and prints how to set a shortcut.
#
# This is the source install, for running Clippy straight from a checkout on
# any Wayland distro. If your distro has a package, prefer it:
#   Debian/Ubuntu/Pop!_OS   make deb    (or the APT repo -- see the README)
#   Arch/Omarchy            make arch
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/clippy"

echo "==> [1/5] Installing system dependencies (sudo required)"
if command -v pacman >/dev/null 2>&1; then
    # Arch, Omarchy, Manjaro, EndeavourOS.
    sudo pacman -S --needed --noconfirm \
        wl-clipboard \
        python-gobject \
        gtk3 \
        gtk-layer-shell \
        libayatana-appindicator \
        libnotify \
        pipewire
    # LAN sync is optional; don't fail the whole install if it can't be had.
    sudo pacman -S --needed --noconfirm \
        python-pynacl python-zeroconf python-spake2 \
        || echo "    WARN: sync deps unavailable; LAN sync will stay off"
elif command -v apt-get >/dev/null 2>&1; then
    # Debian, Ubuntu, Pop!_OS.
    sudo apt-get update
    sudo apt-get install -y \
        wl-clipboard \
        python3-gi \
        gir1.2-gtk-3.0 \
        gir1.2-gtklayershell-0.1 \
        libgtk-layer-shell0 \
        gir1.2-ayatanaappindicator3-0.1 \
        libayatana-appindicator3-1 \
        libnotify-bin \
        pipewire-bin
    sudo apt-get install -y python3-nacl python3-zeroconf \
        || echo "    WARN: sync deps unavailable; LAN sync will stay off"
elif command -v dnf >/dev/null 2>&1; then
    # Fedora and friends.
    sudo dnf install -y \
        wl-clipboard \
        python3-gobject \
        gtk3 \
        gtk-layer-shell \
        libayatana-appindicator-gtk3 \
        libnotify \
        pipewire-utils
    sudo dnf install -y python3-pynacl python3-zeroconf \
        || echo "    WARN: sync deps unavailable; LAN sync will stay off"
else
    echo "    No supported package manager found (pacman/apt-get/dnf)."
    echo "    Install these yourself, then re-run:"
    echo "      wl-clipboard, PyGObject, GTK 3, gtk-layer-shell,"
    echo "      libayatana-appindicator, libnotify"
    echo "    Optional, for LAN sync: PyNaCl, zeroconf, spake2"
    exit 1
fi

echo "==> [2/5] Verifying GTK + layer-shell bindings"
python3 - <<'PY'
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, GtkLayerShell  # noqa
print("    OK: GTK3 + GtkLayerShell")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3  # noqa
    print("    OK: AyatanaAppIndicator3 (tray icon)")
except Exception as e:
    print(f"    WARN: tray bindings unavailable ({e}); panel ⚙ still opens settings")
PY

echo "==> [3/5] Installing launcher at $BIN and icon"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/clippy/icons"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$PROJECT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m clippy "\$@"
EOF
chmod +x "$BIN"
cp -f "$PROJECT_DIR/data/icons/clippy.png" "$HOME/.local/share/clippy/icons/clippy.png"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "    NOTE: add ~/.local/bin to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

echo "==> [4/5] Autostart, icons, application entry"
"$BIN" install-autostart
"$BIN" install-icons
"$BIN" install-desktop
# Refresh icon caches so the tray host picks up 'clippy' immediately.
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true

echo "==> [5/5] Starting the daemon"
if "$BIN" status 2>/dev/null | grep -q "daemon:  running"; then
    "$BIN" quit 2>/dev/null || true; sleep 1
fi
nohup "$BIN" daemon >/tmp/clippy.log 2>&1 &
sleep 1
echo "    daemon started (log: /tmp/clippy.log)"


echo
echo "============================================================"
# `status` ends with the firewall warning when one is due: LAN sync is
# inbound, and a default-on firewall drops it while this side still looks
# perfectly healthy.
"$BIN" status || true
echo
echo "A paperclip should appear in your panel's tray. Open it → Settings"
echo "to pick a shortcut, or run:  clippy setup-shortcut"
