#!/usr/bin/env bash
# Stops Clippy and removes the user-level install (launcher, autostart, app
# entry, icons, and the COSMIC shortcut it registered).
# History/settings under ~/.local/share/clippy and ~/.config/clippy are kept
# unless --purge is given.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/clippy"

echo "==> Stopping daemon + clipboard watcher"
"$BIN" quit 2>/dev/null || true
sleep 1
pkill -f "wl-paste --watch .* -m clippy _store" 2>/dev/null || true
pkill -f "python3 -m clippy daemon" 2>/dev/null || true

echo "==> Removing the COSMIC shortcut Clippy registered (backup is kept)"
PYTHONPATH="$PROJECT_DIR" python3 -c \
    "from clippy import setup; print('removed' if setup.remove_cosmic_shortcut() else 'none')" \
    2>/dev/null || echo "    (skipped)"

echo "==> Removing launcher, autostart, app entry, icons"
rm -f "$BIN"
rm -f "$HOME/.config/autostart/clippy.desktop"
rm -f "$HOME/.local/share/applications/clippy.desktop"
rm -f "$HOME"/.local/share/icons/hicolor/*/apps/clippy.png
rm -f "${XDG_RUNTIME_DIR:-/tmp}/clippy.sock" 2>/dev/null || true

# Refresh caches so the icon/entry disappear from the shell.
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
    echo "==> Purging history and settings"
    rm -rf "$HOME/.local/share/clippy" "$HOME/.config/clippy"
fi

echo
echo "Done. (System packages like wl-clipboard were left installed.)"
echo "A backup of your original COSMIC shortcuts, if Clippy ever edited them, is at:"
echo "  ~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom.clippy.bak"
[ "${1:-}" = "--purge" ] || echo "History/settings kept; re-run with --purge to remove them too."
