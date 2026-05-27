#!/usr/bin/env bash
# Installs system dependencies, a launcher, autostart, and the icon, then
# starts the Clippy daemon and prints how to set a shortcut.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/clippy"

echo "==> [1/5] Installing system dependencies (sudo required)"
sudo apt-get update
sudo apt-get install -y \
    wl-clipboard \
    python3-gi \
    gir1.2-gtk-3.0 \
    gir1.2-gtklayershell-0.1 \
    libgtk-layer-shell0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    pipewire-bin

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
echo "Done. A paperclip icon should appear in your COSMIC panel."
echo "Open it → Settings to pick your shortcut (e.g. Super+V),"
echo "or run:  clippy setup-shortcut"
